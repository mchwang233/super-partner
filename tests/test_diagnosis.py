import asyncio

import pytest
from super_partner.config import settings
from super_partner.db import Database
from super_partner.diagnosis import parse
from super_partner.executor import Executor
from super_partner.models import (
    CapabilityScore,
    Diagnosis,
    ExecutionRun,
    ModelProfile,
    NodeAttempt,
    NodeStatus,
    RunStatus,
    TaskNode,
    TaskPlan,
)
from super_partner.providers import ProviderRegistry, ProviderResult
from super_partner.quota import QuotaTracker
from super_partner.routing import pick_diagnoser

ORIGIN = {"origin": "http://localhost:5173"}
REPORT = "## 问题定位\n第 1 步失败\n## 根本原因\n缺少 node\n## 建议操作\n换模型\nNEXT_STEP: 先确认 node 可用再运行检查"


def model(model_id: str, planning: float, channel: str = "api") -> ModelProfile:
    return ModelProfile(id=model_id, provider=model_id.split(":")[0], display_name=model_id, channel=channel,
                        context_window=1_000_000, input_per_million=1, output_per_million=1,
                        execution_priority=0 if channel == "cli" else 100,
                        scores=CapabilityScore(coding=90, planning=planning, reliability=90))


MODELS = {m.id: m for m in [model("x:api", 99), model("cli:claude", 90, "cli")]}


class Providers:
    def __init__(self):
        self.calls = []

    def configured_models(self, ids):
        return set(ids)

    async def complete(self, model_id, prompt, workspace=None, **_kwargs):
        self.calls.append(model_id)
        if prompt.startswith("You are diagnosing"):
            return ProviderResult(REPORT, 0, 0, model_id)
        raise RuntimeError("node is not installed")


def test_diagnosis_prefers_a_membership_cli_then_the_best_planner():
    assert pick_diagnoser(list(MODELS.values()), set(MODELS)).id == "cli:claude"
    assert pick_diagnoser(list(MODELS.values()), {"x:api"}).id == "x:api"
    assert pick_diagnoser(list(MODELS.values()), set()) is None


def test_the_advice_and_status_lines_are_split_from_the_report():
    report, advice, status = parse(REPORT)
    assert report.endswith("换模型") and advice == "先确认 node 可用再运行检查" and status is None
    assert parse(REPORT + "\nSTATUS: BLOCKED")[1:] == ("先确认 node 可用再运行检查", "blocked")
    assert parse("## 修复内容\n装好了 node\n\nstatus: Fixed") == ("## 修复内容\n装好了 node", None, "fixed")
    assert parse("模型漏了一步\nNEXT_STEP: 先写测试\nSTATUS: RETRY") == ("模型漏了一步", "先写测试", "retry")
    assert parse("执行模型额度耗尽\nSTATUS: QUOTA_EXHAUSTED") == ("执行模型额度耗尽", None, "quota_exhausted")
    assert parse("no advice here") == ("no advice here", None, None)


def one_node_run(database: Database) -> None:
    node = TaskNode(id="n", title="Check", instructions="run node --check", capability="coding", model_id="x:api")
    database.save_run(ExecutionRun(id="r", prompt="p", status=RunStatus.QUEUED, budget_usd=1, use_cli=True,
                                   plan=TaskPlan(summary="s", nodes=[node])))


FIXED = "## 修复内容\n装好了 node，放在工具目录的 bin 里\nSTATUS: FIXED"
RETRY = "模型没有先确认 node 是否可用\nNEXT_STEP: 先运行 node --version\nSTATUS: RETRY"


class Repairer(Providers):
    """The step fails until the environment is repaired; each diagnosis answers ``reply``."""

    def __init__(self, reply: str, repair_works: bool = True):
        super().__init__()
        self.reply, self.repair_works, self.repaired = reply, repair_works, False
        self.repairs, self.step_prompts = [], []

    async def complete(self, model_id, prompt, workspace=None, **kwargs):
        self.calls.append(model_id)
        if "repair whatever blocks it outside the task" in prompt:
            self.repairs.append((prompt, kwargs))
            self.repaired = self.repair_works
            return ProviderResult(self.reply, 0, 0, model_id)
        self.step_prompts.append(prompt)
        if self.repaired:
            return ProviderResult("checked", 0, 0, model_id)
        raise RuntimeError("node: command not found")


async def run_repair(tmp_path, monkeypatch, providers, attempts=2):
    monkeypatch.setattr(settings, "auto_diagnose", True)
    monkeypatch.setattr(settings, "workspace_root", tmp_path / "workspaces")
    monkeypatch.setattr(settings, "max_node_attempts", attempts)
    monkeypatch.setattr(settings, "review_nodes", False)
    database = Database(tmp_path / "test.db")
    one_node_run(database)
    await Executor(database, providers, MODELS).execute("r")
    return database, database.get_run("r")


async def test_every_failure_is_diagnosed_and_a_repaired_environment_runs_the_step_again(tmp_path, monkeypatch):
    providers = Repairer(FIXED)
    database, run = await run_repair(tmp_path, monkeypatch, providers)

    node = run.plan.nodes[0]
    assert run.status == RunStatus.SUCCEEDED and node.status == NodeStatus.SUCCEEDED
    # The first failure is diagnosed at once; after the repair the step's own model passes.
    assert [(a.model_id, a.outcome) for a in node.attempts] == [("x:api", "error"), ("x:api", "passed")]
    [diagnosis] = run.diagnoses
    assert (diagnosis.trigger, diagnosis.model_id, diagnosis.status, diagnosis.verdict) == ("auto", "cli:claude", "done", "fixed")
    assert diagnosis.report.startswith("## 修复内容") and node.environment_fixes == 1
    [(prompt, kwargs)] = providers.repairs
    assert kwargs["repair"] is True and str((tmp_path / "data" / "tools").resolve()) in prompt
    assert "Attempt 1 of step 'Check'" in prompt and "node: command not found" in prompt
    assert "环境已修复" in providers.step_prompts[-1]
    assert any(e.kind == "node.retry" and e.data["reason"] == "environment_fixed" for e in database.events("r"))


async def test_a_blocked_step_pauses_the_task_at_once_with_the_report(tmp_path, monkeypatch):
    reply = REPORT + "\nUSER_ACTION: ROOT: 管理员需挂载只读系统中缺失的设备；用户目录安装无法解决\nSTATUS: BLOCKED"
    database, run = await run_repair(tmp_path, monkeypatch, Repairer(reply, repair_works=False), attempts=3)

    node = run.plan.nodes[0]
    assert run.status == RunStatus.PAUSED and node.status == NodeStatus.FAILED and len(node.attempts) == 1
    assert "遇到需要你处理的问题" in run.error
    [diagnosis] = run.diagnoses
    assert diagnosis.verdict == "blocked" and "根本原因" in diagnosis.report and diagnosis.advice == "先确认 node 可用再运行检查"
    assert diagnosis.user_action.startswith("ROOT:")
    events = database.events("r")
    assert (events[-1].kind, events[-1].data["reason"]) == ("approval.required", "diagnosis_unresolved")


async def test_a_blocked_suggestion_without_a_human_dependency_keeps_working(tmp_path, monkeypatch):
    providers = Repairer(REPORT + "\nSTATUS: BLOCKED")
    _database, run = await run_repair(tmp_path, monkeypatch, providers)
    assert run.status == RunStatus.SUCCEEDED
    assert run.diagnoses[0].verdict == "retry"
    assert "诊断未指出必须由用户提供" in providers.step_prompts[-1]


class QuotaDiagnosis(Providers):
    """The raw error is new to the detector; diagnosis recognizes the execution limit."""

    def __init__(self, *, backup=True, legacy=False, kind="quota"):
        super().__init__()
        self.quota = QuotaTracker()
        self.backup, self.legacy, self.work_calls = backup, legacy, []
        self.kind = kind

    def configured_models(self, ids):
        return {mid for mid in ids if not self.quota.exhausted(mid)
                and (self.backup or mid == "x:api")}

    async def complete(self, model_id, prompt, workspace=None, **kwargs):
        self.calls.append(model_id)
        if kwargs.get("repair"):
            status = "BLOCKED" if self.legacy else "QUOTA_EXHAUSTED" if self.kind == "quota" else "MODEL_UNAVAILABLE"
            return ProviderResult(f"执行模型 x:api 的请求次数已经用完。\nSTATUS: {status}", 0, 0, model_id)
        self.work_calls.append(model_id)
        if model_id == "x:api":
            raise RuntimeError("You've hit your session limit" if self.legacy else "provider denied request: E_WINDOW_EMPTY")
        return ProviderResult("finished original work", 0, 0, model_id)


@pytest.mark.parametrize("legacy,kind", [(False, "quota"), (True, "quota"), (False, "unavailable")])
async def test_diagnosed_quota_switches_the_execution_model_instead_of_pausing(tmp_path, monkeypatch, legacy, kind):
    providers = QuotaDiagnosis(legacy=legacy, kind=kind)
    database, run = await run_repair(tmp_path, monkeypatch, providers)

    assert run.status == RunStatus.SUCCEEDED
    assert providers.work_calls == ["x:api", "cli:claude"]
    assert providers.quota.exhausted("x:api") and not providers.quota.exhausted("cli:claude")
    node = run.plan.nodes[0]
    assert node.failed_attempts == 0 and node.environment_fixes == 0
    assert [(a.model_id, a.outcome) for a in node.attempts] == [("x:api", kind), ("cli:claude", "passed")]
    [diagnosis] = run.diagnoses
    assert diagnosis.verdict == ("quota_exhausted" if kind == "quota" else "model_unavailable")
    assert providers.quota.classify("x:api", "provider denied request: E_WINDOW_EMPTY") == (None if legacy else kind)
    [switch] = [e for e in database.events("r") if e.kind == "node.model_switched"]
    assert switch.data["diagnosis_id"] == diagnosis.id
    assert switch.data["model_id"] == "x:api" and switch.data["next_model_id"] == "cli:claude"
    assert not any(e.kind == "approval.required" for e in database.events("r"))


async def test_diagnosed_quota_without_a_backup_pauses_for_quota(tmp_path, monkeypatch):
    providers = QuotaDiagnosis(backup=False)
    database, run = await run_repair(tmp_path, monkeypatch, providers)

    assert run.status == RunStatus.PAUSED and run.plan.nodes[0].status == NodeStatus.PENDING
    assert providers.work_calls == ["x:api"]
    assert len(run.plan.nodes[0].attempts) == 1 and run.plan.nodes[0].failed_attempts == 0
    event = database.events("r")[-1]
    assert event.kind == "approval.required" and event.data["reason"] == "quota_exhausted"
    assert event.data["diagnosis_id"] == run.diagnoses[0].id


async def test_an_unknown_limit_learned_from_diagnosis_is_reused_after_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "workspace_root", tmp_path / "workspaces")
    monkeypatch.setattr(settings, "auto_diagnose", True)
    monkeypatch.setattr(settings, "review_nodes", False)
    database = Database(tmp_path / "shared.db")
    providers = QuotaDiagnosis()
    providers.quota = QuotaTracker(database)
    one_node_run(database)
    await Executor(database, providers, MODELS).execute("r")
    assert database.get_run("r").status == RunStatus.SUCCEEDED
    providers.quota.clear("x:api")  # The cooldown ended; a future occurrence has a new task.

    registry = ProviderRegistry(database)
    monkeypatch.setattr(registry, "configured_models", lambda ids: {mid for mid in ids if not registry.quota.exhausted(mid)})
    calls = []

    async def complete(model_id, prompt, *args, **kwargs):
        calls.append(model_id)
        if model_id == "x:api":
            raise RuntimeError("provider denied request: E_WINDOW_EMPTY")
        return ProviderResult("done", 0, 0, model_id)

    monkeypatch.setattr(registry, "_complete", complete)
    one_node_run(database)
    other = database.get_run("r")
    other.id = "another-project"
    database.save_run(other)
    try:
        await Executor(database, registry, MODELS).execute(other.id)
    finally:
        await registry.client.aclose()
    completed = database.get_run(other.id)
    assert completed.status == RunStatus.SUCCEEDED and completed.diagnoses == []
    assert calls == ["x:api", "cli:claude"]


async def test_shared_environment_fixes_are_recorded_and_reused_by_other_projects(tmp_path, monkeypatch):
    class SharedRepairer(Repairer):
        async def complete(self, model_id, prompt, workspace=None, **kwargs):
            if kwargs.get("repair"):
                shared = settings.data_dir / "tools" / "bin" / "check-tool"
                shared.write_text("#!/bin/sh\necho ready\n")
            return await super().complete(model_id, prompt, workspace, **kwargs)

    database, run = await run_repair(tmp_path, monkeypatch, SharedRepairer(FIXED))
    [repair] = Database(tmp_path / "test.db").shared_repairs()
    assert repair["files"] == ["bin/check-tool"] and repair["run_id"] == run.id
    assert any(e.kind == "run.repair_shared" for e in database.events(run.id))

    one_node_run(database)
    other = database.get_run("r")
    other.id = "another-project"
    database.save_run(other)
    providers = Repairer(FIXED)
    await Executor(database, providers, MODELS).execute(other.id)
    assert database.get_run(other.id).status == RunStatus.SUCCEEDED
    prompt, _kwargs = providers.repairs[0]
    assert repair["id"] in prompt and "bin/check-tool" in prompt


async def test_a_retry_verdict_retries_with_its_advice_until_attempts_run_out(tmp_path, monkeypatch):
    providers = Repairer(RETRY, repair_works=False)
    _database, run = await run_repair(tmp_path, monkeypatch, providers)

    node = run.plan.nodes[0]
    assert run.status == RunStatus.PAUSED and len(node.attempts) == 2 and len(run.diagnoses) == 2
    assert "诊断建议：先运行 node --version" in providers.step_prompts[1]
    assert "重试 2 次仍未通过" in run.error and run.diagnoses[-1].verdict == "retry"


async def test_repairs_stop_counting_free_after_two(tmp_path, monkeypatch):
    _database, run = await run_repair(tmp_path, monkeypatch, Repairer(FIXED, repair_works=False))

    # Two free reruns after repairs, then the failures count: two more attempts.
    assert run.status == RunStatus.PAUSED and len(run.plan.nodes[0].attempts) == 4
    assert run.plan.nodes[0].environment_fixes == 2 and "重试 2 次仍未通过" in run.error


class LimitedCli(Repairer):
    """The membership CLI is at its usage limit; the API model repairs instead."""

    async def complete(self, model_id, prompt, workspace=None, **kwargs):
        if model_id == "cli:claude":
            self.calls.append(model_id)
            raise RuntimeError("claude failed: Claude AI usage limit reached")
        return await super().complete(model_id, prompt, workspace, **kwargs)


async def test_a_repair_falls_back_to_the_next_model_when_the_cli_is_unavailable(tmp_path, monkeypatch):
    providers = LimitedCli(FIXED)
    database, run = await run_repair(tmp_path, monkeypatch, providers)

    assert run.status == RunStatus.SUCCEEDED and run.diagnoses[0].model_id == "x:api"
    assert run.diagnoses[0].verdict == "fixed" and providers.repairs[0][1]["act"] is True
    [fallback] = [e for e in database.events("r") if e.kind == "run.diagnosis_fallback"]
    assert (fallback.data["model_id"], fallback.data["next_model_id"]) == ("cli:claude", "x:api")
    assert "usage limit" in fallback.data["error"]


async def test_without_any_execution_model_the_run_pauses_before_calling_it(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "auto_diagnose", True)
    monkeypatch.setattr(settings, "workspace_root", tmp_path / "workspaces")
    monkeypatch.setattr(settings, "max_node_attempts", 2)
    database = Database(tmp_path / "test.db")
    one_node_run(database)
    await Executor(database, Providers(), {}).execute("r")
    run = database.get_run("r")
    assert run.status == RunStatus.PAUSED and not run.plan.nodes[0].attempts
    assert database.events("r")[-1].data["reason"] == "model_unavailable"


async def test_without_any_model_the_diagnosis_says_so(tmp_path):
    database = Database(tmp_path / "test.db")
    run = ExecutionRun(id="r", prompt="p", status=RunStatus.FAILED, budget_usd=1, plan=TaskPlan(summary="s", nodes=[]))
    executor = Executor(database, Providers(), {})
    assert executor.begin_diagnosis(run, "manual") is None
    assert run.diagnoses[0].status == "failed" and "没有可用的诊断模型" in run.diagnoses[0].error


def running_run(status=RunStatus.RUNNING) -> ExecutionRun:
    nodes = [
        TaskNode(id="a", title="A", instructions="a", capability="coding", model_id="cli:claude", status=NodeStatus.RUNNING),
        TaskNode(id="b", title="B", instructions="b", capability="coding", model_id="cli:claude", dependencies=["a"]),
    ]
    return ExecutionRun(id="r1", prompt="p", status=status, budget_usd=1, plan=TaskPlan(summary="s", nodes=nodes))


async def test_pausing_lets_running_steps_finish_and_continuing_keeps_the_same_loop(task_api, monkeypatch):
    api, client = task_api
    monkeypatch.setattr(api, "models", MODELS)
    monkeypatch.setattr(api.providers, "configured_models", lambda ids: set(ids))
    api.db.save_run(running_run())
    response = await client.post("/api/tasks/r1/pause", headers=ORIGIN)
    assert response.status_code == 200 and response.json()["status"] == "paused"
    event = api.db.events("r1")[-1]
    assert (event.kind, event.data) == ("approval.required", {"reason": "user_paused", "running": ["a"]})
    assert (await client.post("/api/tasks/r1/pause", headers=ORIGIN)).status_code == 409

    # The executor loop is still waiting for step A, so continuing hands the run back to it.
    loop = asyncio.get_running_loop().create_future()
    monkeypatch.setitem(api.executor.tasks, "r1", loop)
    response = await client.post("/api/tasks/r1/confirm", json={"budget_usd": 1}, headers=ORIGIN)
    assert response.json()["status"] == "running"
    loop.cancel()


async def test_manual_diagnosis_starts_once_and_its_advice_reaches_the_resumed_step(task_api, monkeypatch):
    api, client = task_api
    started = []
    monkeypatch.setattr(api, "models", MODELS)
    monkeypatch.setattr(api.executor, "model_lookup", MODELS)
    monkeypatch.setattr(api.providers, "configured_models", lambda ids: set(ids))
    monkeypatch.setattr(api.executor, "start_diagnosis", lambda run_id, diagnosis_id: started.append(diagnosis_id))
    run = running_run(RunStatus.FAILED)
    run.plan.nodes[0].status = NodeStatus.FAILED
    run.plan.nodes[0].attempts = [NodeAttempt(model_id="cli:claude", outcome="error", reason="node missing")]
    api.db.save_run(run)

    response = await client.post("/api/tasks/r1/diagnose", headers=ORIGIN)
    assert response.status_code == 200
    [diagnosis] = response.json()["diagnoses"]
    assert (diagnosis["trigger"], diagnosis["node_id"], diagnosis["model_id"], diagnosis["status"]) == ("manual", "a", "cli:claude", "running")
    assert started == [diagnosis["id"]]

    pending = asyncio.get_running_loop().create_future()
    monkeypatch.setitem(api.executor.diagnosing, "r1", pending)
    assert (await client.post("/api/tasks/r1/diagnose", headers=ORIGIN)).status_code == 409
    pending.cancel()

    stored = api.db.get_run("r1")
    stored.diagnoses = [Diagnosis(id="d1", trigger="auto", node_id="a", status="done", report="r", advice="先安装 node")]
    api.db.save_run(stored)
    monkeypatch.setattr(api.executor, "start", lambda run_id: None)
    assert (await client.post("/api/tasks/r1/resume", json={"budget_usd": 1}, headers=ORIGIN)).status_code == 200
    assert api.db.get_run("r1").plan.nodes[0].feedback == "node missing\n诊断建议：先安装 node"


async def test_continuing_a_task_paused_by_a_diagnosis_reruns_the_failed_step_with_its_advice(task_api, monkeypatch):
    api, client = task_api
    monkeypatch.setattr(api, "models", MODELS)
    monkeypatch.setattr(api.providers, "configured_models", lambda ids: set(ids))
    monkeypatch.setattr(api.executor, "start", lambda run_id: None)
    run = running_run(RunStatus.PAUSED)
    node = run.plan.nodes[0]
    node.status, node.failed_attempts, node.environment_fixes = NodeStatus.FAILED, 3, 2
    node.attempts = [NodeAttempt(model_id="cli:claude", outcome="error", reason="node missing")]
    run.error = "步骤「A」诊断认为需要你先处理，任务已暂停。"
    run.diagnoses = [Diagnosis(id="d1", trigger="auto", node_id="a", status="done", report="r", advice="先安装 node", verdict="blocked")]
    api.db.save_run(run)

    response = await client.post("/api/tasks/r1/confirm", json={"budget_usd": 1}, headers=ORIGIN)
    assert response.status_code == 200 and response.json()["status"] == "queued"
    stored = api.db.get_run("r1")
    assert stored.error is None and (stored.plan.nodes[0].status, stored.plan.nodes[0].failed_attempts) == (NodeStatus.PENDING, 0)
    assert stored.plan.nodes[0].environment_fixes == 0
    assert stored.plan.nodes[0].feedback == "node missing\n诊断建议：先安装 node"
