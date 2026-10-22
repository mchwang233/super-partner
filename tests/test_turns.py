import asyncio

import pytest
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


@pytest.fixture(autouse=True)
def turn_limit(monkeypatch):
    """These tests are about the optional turn limit, which is off by default."""
    monkeypatch.setattr(settings, "api_agent_max_turns", 40)


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


async def test_artifact_turn_limit_accumulates_across_retries_and_cannot_be_reset_by_resume(tmp_path, monkeypatch):
    from artifact_helpers import contract_node, setup_executor

    async def no_output(*args):
        pass

    node = contract_node(contract={"max_turns": 2})
    executor, db, providers = setup_executor(tmp_path, monkeypatch, [node], no_output)
    monkeypatch.setattr(settings, "max_node_attempts", 10)
    await executor.execute("artifacts")
    run = db.get_run("artifacts")
    node = run.plan.nodes[0]
    assert node.status == NodeStatus.BLOCKED and node.contract_turns == 2
    assert providers.calls == ["vdb", "vdb"]
    assert [a.outcome for a in node.attempts] == ["unverified", "stopped"]
    assert "轮次上限" in node.review and node.attempts[-1].tool_calls
    assert run.actual_cost_usd == pytest.approx(sum(a.cost_usd for a in node.attempts))
    run.status, node.status = RunStatus.QUEUED, NodeStatus.PENDING
    node.failed_attempts = 0
    db.save_run(run)
    await Executor(db, providers, executor.model_lookup).execute("artifacts")
    assert providers.calls == ["vdb", "vdb"]
    assert db.get_run("artifacts").plan.nodes[0].attempts[-1].outcome == "stopped"


async def test_artifact_cost_limit_stops_instead_of_budget_pause_or_retry(tmp_path, monkeypatch):
    from artifact_helpers import contract_node, setup_executor

    async def no_output(*args):
        pass

    executor, db, providers = setup_executor(tmp_path, monkeypatch,
                                           [contract_node(contract={"max_cost_usd": 0.002})], no_output)
    monkeypatch.setattr(settings, "auto_diagnose", True)
    await executor.execute("artifacts")
    run = db.get_run("artifacts")
    assert run.status == RunStatus.INCOMPLETE
    assert providers.calls == ["vdb"] and run.actual_cost_usd == pytest.approx(0.002)
    node = run.plan.nodes[0]
    assert node.attempts[-1].outcome == "stopped" and "费用上限" in node.review
    assert node.contract_cost_usd == pytest.approx(0.002)


async def test_artifact_quota_error_at_declared_limit_stops_before_switching_models(tmp_path, monkeypatch):
    from datetime import datetime, timezone

    from artifact_helpers import contract_node, setup_executor
    from super_partner.quota import ModelUnavailable

    async def unavailable(node, workspace, kwargs):
        error = ModelUnavailable("test:worker", datetime.now(timezone.utc), "quota exhausted")
        error.usage = ProviderResult("", 1000, 1000, "test:worker", turns=1)
        raise error

    executor, db, providers = setup_executor(tmp_path, monkeypatch,
                                           [contract_node(contract={"max_turns": 1})], unavailable)
    await executor.execute("artifacts")
    run = db.get_run("artifacts")
    assert run.status == RunStatus.INCOMPLETE
    assert providers.calls == ["vdb"] and run.plan.nodes[0].attempts[-1].outcome == "stopped"
    assert run.plan.nodes[0].attempts[-1].cost_usd == pytest.approx(0.002)


async def test_tool_allowance_survives_retry_switch_rework_and_restore(tmp_path, monkeypatch):
    from datetime import datetime, timezone

    from artifact_helpers import contract_node, setup_executor
    from super_partner.quota import ModelUnavailable

    extra_calls = []

    async def work(node, workspace, kwargs):
        await kwargs["spend_ok"].begin_tool()
        extra_calls.append(node.id)
        (workspace / "partial.txt").write_text("keep partial work")
        providers.authorized.remove("test:worker")
        error = ModelUnavailable("test:worker", datetime.now(timezone.utc), "quota exhausted")
        error.usage = ProviderResult("", 1000, 1000, "test:worker", turns=1)
        raise error

    executor, db, providers = setup_executor(tmp_path, monkeypatch,
                                           [contract_node(contract={"max_turns": 3})], work)
    backup = executor.model_lookup["test:worker"].model_copy(update={"id": "test:backup"})
    executor.model_lookup[backup.id] = backup
    providers.authorized.add(backup.id)
    await executor.execute("artifacts")
    run = db.get_run("artifacts")
    node = run.plan.nodes[0]
    assert run.status == RunStatus.INCOMPLETE
    assert [a.model_id for a in node.attempts] == ["test:worker", "test:backup"]
    assert node.attempts[-1].outcome == "stopped"
    assert node.contract_tool_calls == 3 and node.contract_turns == 2
    assert len(providers.actual_calls) + len(extra_calls) == 3
    assert "工具调用上限" in node.review
    assert run.actual_cost_usd == pytest.approx(0.004)
    assert (settings.workspace_root / run.folder_id / "partial.txt").read_text() == "keep partial work"
    # A manual resume, rework generation and a newly constructed executor retain both counters.
    run.status, node.status, node.failed_attempts = RunStatus.QUEUED, NodeStatus.PENDING, 0
    node.generation += 1
    db.save_run(run)
    await Executor(db, providers, executor.model_lookup).execute(run.id)
    restored = db.get_run(run.id).plan.nodes[0]
    assert restored.contract_tool_calls == 3 and restored.contract_turns == 2
    assert len(restored.attempts) == 2 and len(providers.actual_calls) + len(extra_calls) == 3
    blocked = [e.data for e in db.events(run.id) if e.kind == "node.blocked"][-1]
    assert blocked["started"] is False
    assert blocked["actual_counts"]["vdb"]["tool_calls"] == 3
    assert blocked["actual_counts"]["vdb"]["requests"] == 2


async def test_parallel_delegated_tool_calls_share_atomic_ancestor_allowance(tmp_path, monkeypatch):
    from artifact_helpers import contract_node, setup_executor
    from super_partner.budget import StepLimitExceeded

    parent = contract_node("parent", contract={"max_turns": 3})
    children = [TaskNode(id=f"child-{i}", title="child", instructions="work", capability="coding",
                         model_id="test:worker", delegation_parent_id=parent.id) for i in range(6)]
    executor, db, _ = setup_executor(tmp_path, monkeypatch, [parent, *children])
    run = db.get_run("artifacts")
    started = []
    # Queue the requests on the same lock to exercise the check/debit race explicitly.
    lock = executor.locks.setdefault(run.id, asyncio.Lock())
    await lock.acquire()

    async def invoke(child, budget):
        await budget.begin_tool()
        started.append(child.id)
        await asyncio.sleep(0)
        return ProviderResult("done", 0, 0, "test:worker")

    pending = [asyncio.create_task(executor._charged(
        run, child, child.model_id, lambda budget, child=child: invoke(child, budget), work=True)) for child in children]
    await asyncio.sleep(0)
    lock.release()
    results = await asyncio.gather(*pending, return_exceptions=True)
    assert len(started) == 3 and sum(isinstance(item, StepLimitExceeded) for item in results) == 3
    assert db.get_run(run.id).plan.nodes[0].contract_tool_calls == 3
    assert db.get_run(run.id).plan.nodes[0].contract_turns == 0
    restored = Executor(db, executor.providers, executor.model_lookup)
    with pytest.raises(StepLimitExceeded, match="工具调用上限"):
        await restored._charged(run, children[-1], "test:worker", lambda budget: invoke(children[-1], budget), work=True)
    assert len(started) == 3


async def test_request_hard_cap_still_applies_when_no_tools_are_called(tmp_path, monkeypatch):
    from artifact_helpers import contract_node, setup_executor
    from super_partner.budget import StepLimitExceeded

    executor, db, _ = setup_executor(tmp_path, monkeypatch, [contract_node(contract={"max_turns": 2})])
    run = db.get_run("artifacts")
    node = run.plan.nodes[0]
    requests = []

    async def request(budget):
        await budget.begin_turn()
        requests.append(True)
        return ProviderResult("no tools", 0, 0, "test:worker")

    for _ in range(2):
        await executor._charged(run, node, node.model_id, request, work=True)
    with pytest.raises(StepLimitExceeded, match="轮次上限"):
        await executor._charged(run, node, node.model_id, request, work=True)
    saved = db.get_run(run.id).plan.nodes[0]
    assert len(requests) == saved.contract_turns == 2 and saved.contract_tool_calls == 0


def test_legacy_task_records_load_with_separate_tool_counter():
    from artifact_helpers import contract_node

    legacy = contract_node().model_dump(exclude={"contract_tool_calls"})
    legacy["contract_turns"] = 2
    loaded = TaskNode.model_validate(legacy)
    assert loaded.contract_turns == 2 and loaded.contract_tool_calls == 0
