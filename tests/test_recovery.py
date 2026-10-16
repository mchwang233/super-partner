"""After failures: a model that failed a step twice in a row gives way, and a diagnosis can end a
step that cannot be done here instead of retrying it."""
import pytest
from super_partner import diagnosis, recovery
from super_partner.config import settings
from super_partner.db import Database
from super_partner.executor import Executor
from super_partner.models import (
    ExecutionRun,
    NodeAttempt,
    NodeStatus,
    RunStatus,
    TaskNode,
    TaskPlan,
)
from super_partner.providers import ProviderResult
from test_turns import model
from test_unable import Workers, configure

# API-first: the flash API model is tried before the stronger CLI, and the pro API model is weaker.
MODELS = [model("x:flash", 90), model("x:pro", 85), model("cli:codex", 95, "cli")]
CONFIGURED = {item.id for item in MODELS}


def step(*attempts) -> TaskNode:
    return TaskNode(id="n", title="Build", instructions="build", capability="coding", model_id="x:flash",
                    attempts=[NodeAttempt(model_id=m, outcome=o) for m, o in attempts])


@pytest.fixture(autouse=True)
def api_first(monkeypatch):
    monkeypatch.setattr(settings, "subtasks_prefer_api", True)


def test_one_failure_keeps_the_strongest_model():
    assert recovery.retry_model(step(("x:flash", "rejected")), MODELS, CONFIGURED).id == "x:flash"


def test_two_failures_in_a_row_hand_the_step_to_the_best_other_model():
    node = step(("x:flash", "rejected"), ("x:flash", "error"))
    assert recovery.failed_models(node) == {"x:flash"}
    assert recovery.retry_model(node, MODELS, CONFIGURED).id == "cli:codex"
    # The new model failing once does not send the step back to the one that failed twice.
    node.attempts.append(NodeAttempt(model_id="cli:codex", outcome="rejected"))
    assert recovery.retry_model(node, MODELS, CONFIGURED).id == "cli:codex"
    node.attempts.append(NodeAttempt(model_id="cli:codex", outcome="rejected"))
    assert recovery.retry_model(node, MODELS, CONFIGURED).id == "x:pro"


def test_running_out_of_turns_counts_whatever_the_model_claimed():
    node = step(("x:flash", "unable"), ("x:flash", "unable"))
    assert recovery.failed_models(node) == set()
    for attempt in node.attempts:
        attempt.turn_limit = True
    assert recovery.failed_models(node) == {"x:flash"}
    assert recovery.retry_model(node, MODELS, CONFIGURED).id == "cli:codex"


def test_only_the_models_own_failures_count():
    # Provider limits, cancellations and unverified reviews say nothing about the model's work.
    node = step(("x:flash", "rejected"), ("x:flash", "quota"), ("x:flash", "cancelled"), ("x:flash", "unverified"))
    assert recovery.failed_models(node) == set()
    # A passed attempt (before rework) starts the count again.
    node = step(("x:flash", "rejected"), ("x:flash", "passed"), ("x:flash", "verify_failed"))
    assert recovery.failed_models(node) == set()


@pytest.mark.parametrize("outcome, status, fixes, action", [
    ("rejected", "platform_pending", 0, "hold_for_platform"),
    ("rejected", "needs_input", 0, "ask_user"),
    ("error", "quota_exhausted", 0, "switch_model"),
    ("rejected", "quota_exhausted", 0, "retry"),
    ("rejected", "fixed", 0, "retry_fixed"),
    ("rejected", "fixed", recovery.MAX_ENVIRONMENT_FIXES, "retry"),
    ("rejected", "unable", 0, "unable"),
    ("unable", "blocked", 0, "unable"),
    ("unable", "retry", 0, "retry"),
    ("rejected", "blocked", 0, "pause"),
    ("verify_failed", "retry", 0, "pause_rework"),
    ("rejected", None, 0, "retry"),
])
def test_what_a_diagnosis_means_for_the_step(outcome, status, fixes, action):
    assert recovery.after_diagnosis(outcome, status, has_model=True, environment_fixes=fixes) == action


async def test_a_step_failing_twice_on_an_api_model_moves_to_a_cli(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "workspace_root", tmp_path / "workspaces")
    monkeypatch.setattr(settings, "review_nodes", False)
    monkeypatch.setattr(settings, "auto_diagnose", False)
    monkeypatch.setattr(settings, "max_node_attempts", 3)
    database = Database(tmp_path / "test.db")
    database.save_run(ExecutionRun(id="r", prompt="p", status=RunStatus.QUEUED, budget_usd=1, use_cli=True,
                                   plan=TaskPlan(summary="s", nodes=[step()])))
    calls = []

    class Providers:
        def configured_models(self, ids):
            return set(ids)

        async def complete(self, model_id, prompt, workspace=None, **kwargs):
            calls.append(model_id)
            if model_id == "x:flash":
                raise RuntimeError("wrote nothing")
            return ProviderResult("done", 0, 0, model_id)

    await Executor(database, Providers(), {item.id: item for item in MODELS}).execute("r")
    node = database.get_run("r").plan.nodes[0]
    assert calls == ["x:flash", "x:flash", "cli:codex"] and node.status == NodeStatus.SUCCEEDED


async def test_a_diagnosis_can_end_a_step_that_cannot_be_done_here(tmp_path, monkeypatch):
    database = configure(tmp_path, monkeypatch, diagnose=True)
    unable = ("沙箱里没有 iverilog，没有 root 也装不上。\nUSER_ACTION: ROOT: 安装 iverilog\nSTATUS: UNABLE")
    workers = Workers(deploy="部署失败：命令不存在", diagnosis=unable)
    original = workers.complete

    async def complete(model_id, prompt, workspace=None, **kwargs):
        if prompt.startswith("Task: 部署上线") and not kwargs.get("repair"):
            workers.calls.append("部署上线")
            raise RuntimeError("iverilog: command not found")
        return await original(model_id, prompt, workspace, **kwargs)

    workers.complete = complete
    await Executor(database, workers, {item.id: item for item in MODELS} | {
        m.id: m for m in [model("x:api", 90)]}).execute("r")
    run = database.get_run("r")
    deploy = next(item for item in run.plan.nodes if item.id == "deploy")
    assert run.status == RunStatus.INCOMPLETE and deploy.status == NodeStatus.UNABLE
    assert "需要你处理：ROOT: 安装 iverilog" in deploy.unable_reason
    assert workers.calls.count("部署上线") == 1


def test_a_diagnosis_is_told_when_a_step_keeps_failing(tmp_path):
    run = ExecutionRun(id="r", prompt="p", status=RunStatus.RUNNING, budget_usd=1,
                       plan=TaskPlan(summary="s", nodes=[step(("x:flash", "rejected"), ("x:flash", "rejected"))]))
    node = run.plan.nodes[0]
    text = diagnosis.repair_prompt(run, node, str(tmp_path), "/tools", [], [])
    assert "failed 2 times in a row" in text and "STATUS: UNABLE" in text
    node.attempts = node.attempts[:1]
    assert "times in a row" not in diagnosis.repair_prompt(run, node, str(tmp_path), "/tools", [], [])
