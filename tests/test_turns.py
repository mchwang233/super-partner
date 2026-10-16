from super_partner.config import settings
from super_partner.db import Database
from super_partner.executor import Executor, turns_for
from super_partner.models import (
    CapabilityScore,
    ExecutionRun,
    ModelProfile,
    NodeStatus,
    RunStatus,
    TaskNode,
    TaskPlan,
)
from super_partner.providers import ProviderResult


def model(model_id: str, coding: float, channel: str = "api") -> ModelProfile:
    return ModelProfile(id=model_id, provider=model_id.split(":")[0], display_name=model_id, channel=channel,
                        context_window=1_000_000, input_per_million=1, output_per_million=1,
                        execution_priority=0 if channel == "cli" else 100,
                        scores=CapabilityScore(coding=coding, planning=80, reliability=90))


MODELS = {m.id: m for m in [model("x:flash", 90), model("cli:claude", 96, "cli")]}


class Providers:
    """The API model always runs out of turns; the CLI finishes."""

    def __init__(self):
        self.calls = []

    def configured_models(self, ids):
        return set(ids)

    async def complete(self, model_id, prompt, workspace=None, **kwargs):
        self.calls.append((model_id, kwargs.get("max_turns")))
        if model_id == "cli:claude":
            return ProviderResult("done", 0, 0, model_id)
        log = kwargs["tool_log"]
        log += [{"turn": 1, "tool": "read_file", "detail": "app.js", "ok": True},
                {"turn": 2, "tool": "run_command", "detail": "pytest", "ok": False}]
        return ProviderResult("进度：写完了 app.js，测试还没过", 10, 10, model_id, turns=kwargs["max_turns"], turn_limit=True)


def test_harder_nodes_get_more_turns(monkeypatch):
    monkeypatch.setattr(settings, "api_agent_max_turns", 40)
    turns = {d: turns_for(TaskNode(id="n", title="t", instructions="i", capability="coding", difficulty=d))
             for d in ("easy", "normal", "hard")}
    assert turns == {"easy": 40, "normal": 60, "hard": 80}


async def test_a_model_out_of_turns_twice_makes_way_for_a_stronger_one(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "workspace_root", tmp_path / "workspaces")
    monkeypatch.setattr(settings, "review_nodes", False)
    monkeypatch.setattr(settings, "max_node_attempts", 3)
    database = Database(tmp_path / "test.db")
    node = TaskNode(id="n", title="Build", instructions="build", capability="coding", model_id="x:flash", difficulty="hard")
    database.save_run(ExecutionRun(id="r", prompt="p", status=RunStatus.QUEUED, budget_usd=1, use_cli=True,
                                   plan=TaskPlan(summary="s", nodes=[node])))
    providers = Providers()
    await Executor(database, providers, MODELS).execute("r")

    node = database.get_run("r").plan.nodes[0]
    assert node.status == NodeStatus.SUCCEEDED
    # The API-first policy kept the flash model for the first retry; two limits later the CLI takes over.
    assert [call[0] for call in providers.calls] == ["x:flash", "x:flash", "cli:claude"]
    assert providers.calls[0][1] == turns_for(node)
    first = node.attempts[0]
    # Unreviewed, a progress summary is not taken for finished work.
    assert (first.outcome, first.turn_limit, first.turns) == ("rejected", True, turns_for(node))
    assert [(c.turn, c.tool, c.ok) for c in first.tool_calls] == [(1, "read_file", True), (2, "run_command", False)]
    assert node.attempts[-1].model_id == "cli:claude" and not node.attempts[-1].turn_limit


class VerifierProviders(Providers):
    """The build finishes; the flash verifier runs out of turns after writing a passing verdict."""

    async def complete(self, model_id, prompt, workspace=None, **kwargs):
        self.calls.append((model_id, kwargs.get("max_turns")))
        if model_id == "cli:claude":
            return ProviderResult("done\nVERDICT: PASS", 0, 0, model_id)
        return ProviderResult("PASS 63 / FAIL 0，还在做收尾检查\nVERDICT: PASS", 10, 10, model_id,
                              turns=kwargs["max_turns"], turn_limit=True)


async def test_a_verifier_out_of_turns_is_retried_without_sending_back_the_work_it_verifies(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "workspace_root", tmp_path / "workspaces")
    monkeypatch.setattr(settings, "review_nodes", False)
    monkeypatch.setattr(settings, "auto_diagnose", False)
    monkeypatch.setattr(settings, "max_node_attempts", 3)
    database = Database(tmp_path / "test.db")
    build = TaskNode(id="a", title="Build", instructions="build", capability="coding", model_id="cli:claude")
    check = TaskNode(id="v", title="Check", instructions="verify", capability="coding", model_id="x:flash",
                     dependencies=["a"], verifies=["a"])
    database.save_run(ExecutionRun(id="r", prompt="p", status=RunStatus.QUEUED, budget_usd=1, use_cli=True,
                                   plan=TaskPlan(summary="s", nodes=[build, check])))
    executor = Executor(database, VerifierProviders(), MODELS)
    events = []
    emit = executor.emit
    executor.emit = lambda run_id, kind, **data: (events.append(kind), emit(run_id, kind, **data))[-1]
    await executor.execute("r")

    run = database.get_run("r")
    build, check = run.plan.nodes
    assert "run.rework" not in events and run.rework_rounds == 0
    # The verified step keeps its result and generation; only the verifier is retried.
    assert build.status == NodeStatus.SUCCEEDED and build.generation == 0 and len(build.attempts) == 1
    assert [a.outcome for a in check.attempts] == ["rejected", "rejected", "passed"]
    # Two turn limits on the flash model hand the verification to the stronger CLI.
    assert [a.model_id for a in check.attempts] == ["x:flash", "x:flash", "cli:claude"]
    assert check.status == NodeStatus.SUCCEEDED
