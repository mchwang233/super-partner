"""Cross-cutting failures found during the reliability review (no real model calls)."""
import asyncio
import json

import httpx
import pytest
from super_partner import attachments, environment, quality
from super_partner.budget import BudgetExceeded
from super_partner.config import settings
from super_partner.db import Database
from super_partner.executor import Executor, turns_for
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
from super_partner.planner import autonomous_plan
from super_partner.providers import ProviderRegistry, ProviderResult
from super_partner.quota import QuotaTracker


def model(mid="deepseek:primary", score=95, **kwargs):
    return ModelProfile(id=mid, provider=mid.split(":")[0], display_name=mid,
                        context_window=1_000_000, input_per_million=1, output_per_million=1,
                        scores=CapabilityScore(planning=score, coding=score, reliability=score), **kwargs)


def node(nid="n", **kwargs):
    return TaskNode(id=nid, title=nid, instructions=f"work on {nid}", capability="coding",
                    model_id="deepseek:primary", **kwargs)


def run(nodes, status=RunStatus.QUEUED, **kwargs):
    return ExecutionRun(id="r", prompt="review fixture", budget_usd=1, status=status,
                        plan=TaskPlan(summary="fixture", nodes=nodes), **kwargs)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "workspace_root", tmp_path / "workspaces")
    monkeypatch.setattr(settings, "review_nodes", False)
    monkeypatch.setattr(settings, "max_node_attempts", 1)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only")


class Script:
    def __init__(self, reply="done"):
        self.reply, self.calls, self.quota = reply, [], QuotaTracker()

    def configured_models(self, ids):
        return {mid for mid in ids if not self.quota.exhausted(mid)}

    async def complete(self, mid, prompt, workspace=None, **kwargs):
        self.calls.append((mid, kwargs))
        return ProviderResult(self.reply, 100, 0, mid)


def response(text="done", tools=False):
    message = {"content": text}
    if tools:
        message["tool_calls"] = [{"id": "c1", "type": "function", "function": {"name": "list_dir", "arguments": "{}"}}]
    return httpx.Response(200, json={"choices": [{"message": message}],
                                    "usage": {"prompt_tokens": 1000, "completion_tokens": 50}})


async def registry(handler):
    providers = ProviderRegistry()
    await providers.client.aclose()
    providers.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    providers.configured_models = lambda ids: {mid for mid in ids if not providers.quota.exhausted(mid)}
    providers.max_attempts = 1
    return providers


@pytest.mark.parametrize("failure", ["error", "outage", "turn_limit"])
async def test_paid_rounds_are_retained_on_failure_and_model_switch(tmp_path, monkeypatch, failure):
    calls = []

    def handler(request):
        mid = json.loads(request.content)["model"]
        calls.append(mid)
        if mid == "backup":
            return response()
        if calls.count(mid) == 1 or failure == "turn_limit":
            return response(tools=True)
        return httpx.Response(503 if failure == "outage" else 400, text="provider failure")

    monkeypatch.setattr(settings, "api_agent_max_turns", 2)
    providers = await registry(handler)
    models = {m.id: m for m in [model(), model("deepseek:backup", score=90)]}
    database = Database(tmp_path / "db")
    database.save_run(run([node()]))
    try:
        await Executor(database, providers, models).execute("r")
    finally:
        await providers.client.aclose()
    saved = database.get_run("r")
    # Out of turns, the model is paid for every turn plus the one asking for its progress summary.
    expected = {"error": .00105, "outage": .0021, "turn_limit": .00105 * (turns_for(node()) + 1)}[failure]
    assert saved.actual_cost_usd == pytest.approx(expected)
    assert saved.actual_cost_usd == pytest.approx(sum(a.cost_usd for a in saved.plan.nodes[0].attempts))
    if failure == "outage":
        assert saved.status == RunStatus.SUCCEEDED and calls == ["primary", "primary", "backup"]
    else:
        assert saved.status == RunStatus.FAILED


async def test_final_reply_is_checked_against_budget(tmp_path):
    providers = await registry(lambda request: response())
    checked = []

    def spend_ok(mid, incoming, outgoing):
        checked.append(incoming + outgoing)
        return incoming + outgoing < 1000

    try:
        with pytest.raises(BudgetExceeded) as raised:
            await providers.complete("deepseek:primary", "work", tmp_path, spend_ok=spend_ok)
    finally:
        await providers.client.aclose()
    assert checked == [0, 1050]
    assert raised.value.usage.input_tokens == 1000


async def test_budget_refuses_a_request_before_network_and_pauses_without_quality_retry(tmp_path):
    requests = []
    providers = await registry(lambda request: requests.append(request) or response())
    database = Database(tmp_path / "db")
    current = run([node()])
    current.budget_usd = .00001
    database.save_run(current)
    try:
        await Executor(database, providers, {m.id: m for m in [model()]}).execute("r")
    finally:
        await providers.client.aclose()
    saved = database.get_run("r")
    assert requests == [] and saved.actual_cost_usd == 0
    assert saved.status == RunStatus.PAUSED and saved.plan.nodes[0].status == NodeStatus.PENDING
    assert saved.plan.nodes[0].failed_attempts == 0 and saved.plan.nodes[0].attempts[-1].outcome == "budget"


async def test_parallel_requests_share_reservations(tmp_path):
    requested, release = asyncio.Event(), asyncio.Event()
    requests = []

    async def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        requested.set()
        await release.wait()
        return response()

    providers = await registry(handler)
    database = Database(tmp_path / "db")
    current = run([node()])
    current.budget_usd = .0045
    database.save_run(current)
    executor = Executor(database, providers, {m.id: m for m in [model()]})
    first = asyncio.create_task(executor._complete(current, current.plan.nodes[0], "deepseek:primary", "first", tmp_path))
    second = None
    try:
        await asyncio.wait_for(requested.wait(), 2)
        second = asyncio.create_task(executor._complete(current, current.plan.nodes[0], "deepseek:primary", "second", tmp_path))
        await asyncio.sleep(.02)
        assert len(requests) == 1 and 0 < requests[0]["max_tokens"] < settings.api_agent_max_output_tokens
        release.set()
        await asyncio.wait_for(asyncio.gather(first, second), 2)
        assert len(requests) == 2 and requests[1]["max_tokens"] < requests[0]["max_tokens"]
        assert not executor.reservations["r"]
        assert database.get_run("r").actual_cost_usd == pytest.approx(.0021)
    finally:
        release.set()
        await asyncio.gather(*[t for t in (first, second) if t], return_exceptions=True)
        await providers.client.aclose()


async def test_cancellation_keeps_paid_rounds_and_releases_reservations(tmp_path):
    waiting, hold = asyncio.Event(), asyncio.Event()
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return response(tools=True)
        waiting.set()
        await hold.wait()
        return response()

    providers = await registry(handler)
    database = Database(tmp_path / "db")
    database.save_run(run([node()]))
    executor = Executor(database, providers, {m.id: m for m in [model()]})
    executor.start("r")
    try:
        await asyncio.wait_for(waiting.wait(), 2)
        current = database.get_run("r")
        assert current.actual_cost_usd == pytest.approx(.00105)
        current.status = RunStatus.CANCELLED
        database.save_run(current)
        executor.cancel("r")
        with pytest.raises(asyncio.CancelledError):
            await executor.tasks["r"]
        saved = database.get_run("r")
        assert saved.actual_cost_usd == pytest.approx(.00105)
        assert saved.plan.nodes[0].attempts[-1].cost_usd == pytest.approx(.00105)
        assert not executor.reservations["r"]
    finally:
        executor.cancel("r")
        await asyncio.gather(executor.tasks["r"], return_exceptions=True)
        await providers.client.aclose()


async def test_rework_cancels_and_reexecutes_inflight_consumers(tmp_path):
    database = Database(tmp_path / "db")
    executor = Executor(database, Script(), {m.id: m for m in [model()]})
    started, hold = asyncio.Event(), asyncio.Event()
    calls = {key: 0 for key in ["a", "b", "v", "c"]}
    cancelled = []

    async def work(current, item, mid, prompt, fallback):
        calls[item.id] += 1
        if item.id == "a":
            text = "a-v2"
        elif item.id == "b":
            if calls["b"] == 1:
                assert "a-v1" in prompt
                started.set()
                try:
                    await hold.wait()
                except asyncio.CancelledError:
                    cancelled.append("b")
                    raise
            assert "a-v2" in prompt
            text = "b from a-v2"
        elif item.id == "v":
            await started.wait()
            text = "VERDICT: FAIL: a-v1 is wrong" if calls["v"] == 1 else "VERDICT: PASS"
        else:
            text = prompt
        return ProviderResult(text, 0, 0, mid), [], 0, None

    executor._work = work
    database.save_run(run([
        node("a", status=NodeStatus.SUCCEEDED, result="a-v1"),
        node("v", dependencies=["a"], verifies=["a"]),
        node("b", dependencies=["a"]), node("c", dependencies=["b", "v"]),
    ]))
    await asyncio.wait_for(executor.execute("r"), 3)
    saved = database.get_run("r")
    assert saved.status == RunStatus.SUCCEEDED and cancelled == ["b"] and calls["b"] == 2
    assert "b from a-v2" in saved.final_result
    assert saved.plan.nodes[2].attempts[0].outcome == "superseded"


async def test_disabled_model_is_reselected_before_execution(tmp_path):
    providers = Script()
    models = {m.id: m for m in [model(enabled=False), model("deepseek:backup")]}
    database = Database(tmp_path / "db")
    database.save_run(run([node()]))
    await Executor(database, providers, models).execute("r")
    assert [mid for mid, _ in providers.calls] == ["deepseek:backup"]
    assert database.get_run("r").status == RunStatus.SUCCEEDED


async def test_snapshot_failure_after_a_paid_call_keeps_the_attempt_cost(tmp_path, monkeypatch):
    snapshots = []

    def snapshot(root):
        snapshots.append(root)
        if len(snapshots) > 1:
            raise OSError("snapshot failed")
        return {}

    monkeypatch.setattr(quality, "file_state", snapshot)
    database = Database(tmp_path / "db")
    database.save_run(run([node()]))
    await Executor(database, Script(), {m.id: m for m in [model()]}).execute("r")
    saved = database.get_run("r")
    assert saved.status == RunStatus.FAILED
    assert saved.actual_cost_usd == pytest.approx(.0001)
    assert saved.plan.nodes[0].attempts[0].cost_usd == pytest.approx(.0001)


async def test_real_work_never_falls_back_to_a_demo_review(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "review_nodes", True)

    class Reviewer(Script):
        async def complete(self, mid, prompt, workspace=None, **kwargs):
            self.calls.append((mid, kwargs))
            text = '{"passed": true}' if mid.startswith("mock:") else "Unparseable reply"
            return ProviderResult(text, 100, 0, mid)

    providers = Reviewer()
    database = Database(tmp_path / "db")
    database.save_run(run([node()]))
    await Executor(database, providers, {m.id: m for m in [model(), model("mock:local")]}).execute("r")
    assert database.get_run("r").status == RunStatus.FAILED
    assert database.get_run("r").plan.nodes[0].review_passed is None
    assert all(not mid.startswith("mock:") for mid, _ in providers.calls)


async def test_confirm_reselects_disabled_models_before_estimating_budget(task_api, monkeypatch):
    api, client = task_api
    models = {m.id: m for m in [model(enabled=False), model("deepseek:backup")]}
    monkeypatch.setattr(api, "models", models)
    monkeypatch.setattr(api.providers, "configured_models", lambda ids: set(ids))
    monkeypatch.setattr(api.executor, "start", lambda rid: None)
    api.db.save_run(run([node()], status=RunStatus.AWAITING_CONFIRMATION))
    result = await client.post("/api/tasks/r/confirm", json={})
    assert result.status_code == 200
    assert api.db.get_run("r").plan.nodes[0].model_id == "deepseek:backup"


async def test_planner_uses_backup_on_service_outage():
    class Outage(Script):
        async def complete(self, mid, prompt, **kwargs):
            self.calls.append((mid, kwargs))
            if mid == "deepseek:primary":
                raise self.quota.mark(mid, "deepseek returned 503: unavailable", "unavailable")
            return ProviderResult(json.dumps({"summary": "backup plan", "nodes": [
                {"key": "a", "instructions": "first"},
                {"key": "b", "instructions": "second", "dependencies": ["a"]},
            ]}), 0, 0, mid)

    providers = Outage()
    models = [model(), model("deepseek:backup", score=90)]
    plan = await autonomous_plan("review project", models, {m.id for m in models}, providers)
    assert plan.planning_method == "model" and plan.planner_model_id == "deepseek:backup"
    assert [mid for mid, _ in providers.calls] == ["deepseek:primary", "deepseek:backup"]


async def test_manual_diagnosis_switches_model_and_continues_original_work(tmp_path):
    class Repair(Script):
        async def complete(self, mid, prompt, workspace=None, **kwargs):
            self.calls.append((mid, kwargs))
            return ProviderResult("额度用完\nSTATUS: QUOTA_EXHAUSTED" if kwargs.get("repair") else "original task completed", 100, 0, mid)

    providers = Repair()
    database = Database(tmp_path / "db")
    current = run([node(status=NodeStatus.FAILED, attempts=[NodeAttempt(model_id="deepseek:primary", outcome="error", reason="E_NEW_LIMIT")])],
                  status=RunStatus.PAUSED, diagnoses=[Diagnosis(id="d", trigger="manual", node_id="n", model_id="deepseek:backup")])
    database.save_run(current)
    executor = Executor(database, providers, {m.id: m for m in [model(), model("deepseek:backup")]})
    executor.start_diagnosis("r", "d")
    await asyncio.wait_for(executor.diagnosing["r"], 3)
    await asyncio.wait_for(executor.tasks["r"], 3)
    saved = database.get_run("r")
    assert saved.status == RunStatus.SUCCEEDED and saved.final_result == "original task completed"
    assert providers.quota.exhausted("deepseek:primary")
    assert providers.quota.classify("deepseek:primary", "E_NEW_LIMIT") == "quota"
    assert saved.diagnoses[0].verdict == "quota_exhausted" and providers.calls[0][1]["repair"]
    assert saved.actual_cost_usd == pytest.approx(.0002)


@pytest.mark.parametrize("repair", [False, True])
async def test_cancel_stops_manual_diagnosis_and_delete_waits_for_it(task_api, monkeypatch, repair):
    api, client = task_api
    started, hold = asyncio.Event(), asyncio.Event()

    class Delayed(Script):
        async def complete(self, *args, **kwargs):
            started.set()
            await hold.wait()
            return ProviderResult("report", 100, 0, "deepseek:primary")

    executor = Executor(api.db, Delayed(), {m.id: m for m in [model()]})
    monkeypatch.setattr(api, "executor", executor)
    api.db.save_run(run([node()], status=RunStatus.FAILED,
                        diagnoses=[Diagnosis(id="d", trigger="manual", node_id="n" if repair else None, model_id="deepseek:primary")]))
    executor.start_diagnosis("r", "d")
    task = executor.diagnosing["r"]
    await asyncio.wait_for(started.wait(), 2)
    assert (await client.delete("/api/tasks/r")).status_code == 409
    assert (await client.post("/api/tasks/r/cancel")).status_code == 200
    with pytest.raises(asyncio.CancelledError):
        await task
    assert api.db.get_run("r").diagnoses[0].status == "failed"
    assert api.db.get_run("r").actual_cost_usd == 0
    assert (await client.delete("/api/tasks/r")).status_code == 204


async def test_manual_environment_repair_is_shared_and_resumes_the_step(tmp_path):
    class Repair(Script):
        async def complete(self, mid, prompt, workspace=None, **kwargs):
            if kwargs.get("repair"):
                (environment.prepare() / "bin" / "check_env").write_text("#!/bin/sh\necho ready\n")
                return ProviderResult("已安装并验证共享工具。\nSTATUS: FIXED", 100, 0, mid)
            return ProviderResult("original work finished", 100, 0, mid)

    database = Database(tmp_path / "db")
    database.save_run(run([node(status=NodeStatus.FAILED, attempts=[NodeAttempt(outcome="error", reason="missing tool")])],
                         status=RunStatus.FAILED, diagnoses=[Diagnosis(id="d", trigger="manual", node_id="n", model_id="deepseek:primary")]))
    executor = Executor(database, Repair(), {m.id: m for m in [model()]})
    executor.start_diagnosis("r", "d")
    await asyncio.wait_for(executor.diagnosing["r"], 3)
    await asyncio.wait_for(executor.tasks["r"], 3)
    saved = database.get_run("r")
    assert saved.status == RunStatus.SUCCEEDED and saved.diagnoses[0].verdict == "fixed"
    assert database.shared_repairs()[0]["files"] == ["bin/check_env"]
    assert saved.actual_cost_usd == pytest.approx(.0002)


async def test_manual_repair_without_budget_stays_paused_without_consuming_a_retry(tmp_path):
    requests = []
    providers = await registry(lambda request: requests.append(request) or response())
    database = Database(tmp_path / "db")
    current = run([node(status=NodeStatus.FAILED, attempts=[NodeAttempt(outcome="error", reason="missing tool")])],
                  status=RunStatus.PAUSED, diagnoses=[Diagnosis(id="d", trigger="manual", node_id="n", model_id="deepseek:primary")])
    current.budget_usd = .00001
    database.save_run(current)
    executor = Executor(database, providers, {m.id: m for m in [model()]})
    try:
        executor.start_diagnosis("r", "d")
        await asyncio.wait_for(executor.diagnosing["r"], 3)
    finally:
        await providers.client.aclose()
    saved = database.get_run("r")
    assert requests == [] and saved.status == RunStatus.PAUSED and "r" not in executor.tasks
    assert saved.plan.nodes[0].failed_attempts == 0
    assert saved.diagnoses[0].status == "failed" and "预算" in saved.diagnoses[0].error


@pytest.mark.parametrize("name", ["meta.json", "content", "normal.txt"])
def test_upload_content_never_collides_with_metadata(name):
    original = b'{"user_content":"preserve me"}'
    item = attachments.save(name, original)
    assert attachments.get(item.id) == item
    assert attachments.path(item).read_bytes() == original


def test_existing_upload_paths_remain_readable():
    item = attachments.save("old.txt", b"legacy")
    stored = attachments.path(item)
    stored.rename(stored.parent.parent / item.name)
    stored.parent.rmdir()
    assert attachments.path(attachments.get(item.id)).read_bytes() == b"legacy"
