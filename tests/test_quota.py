import os
import time
from datetime import datetime, timedelta, timezone

import pytest
from super_partner.config import settings
from super_partner.db import Database
from super_partner.executor import Executor
from super_partner.models import (
    CapabilityScore,
    Diagnosis,
    ExecutionRun,
    ModelProfile,
    NodeStatus,
    RunStatus,
    TaskNode,
    TaskPlan,
)
from super_partner.providers import ProviderRegistry, ProviderResult
from super_partner.quota import (
    ModelUnavailable,
    QuotaExhausted,
    QuotaTracker,
    is_quota_error,
    reset_time,
)

NOW = datetime(2026, 10, 15, 2, 0, tzinfo=timezone.utc)
ORIGIN = {"origin": "http://localhost:5173"}
SESSION_LIMIT = "claude failed: You've hit your session limit · resets 2pm (Asia/Shanghai)"


@pytest.mark.parametrize("period", ["usage", "session", "weekly", "daily", "monthly"])
def test_membership_limit_variants_are_quota_errors(period):
    assert is_quota_error(f"claude failed: You've hit your {period} limit · resets 2pm (Asia/Shanghai)")


def test_session_reset_uses_the_reported_timezone(monkeypatch):
    previous = os.environ.get("TZ")
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    try:
        assert reset_time(SESSION_LIMIT, NOW) == datetime(2026, 10, 15, 6, tzinfo=timezone.utc)
        after_reset = datetime(2026, 10, 15, 7, tzinfo=timezone.utc)
        assert reset_time(SESSION_LIMIT, after_reset) == datetime(2026, 10, 16, 6, tzinfo=timezone.utc)
        # Unknown zone names keep the existing machine-local fallback.
        assert reset_time(SESSION_LIMIT.replace("Asia/Shanghai", "Unknown/Zone"), NOW).hour == 14
    finally:
        if previous is None:
            monkeypatch.delenv("TZ")
        else:
            monkeypatch.setenv("TZ", previous)
        time.tzset()


def test_quota_messages_are_recognised_and_their_reset_times_read():
    assert is_quota_error("claude failed: Claude AI usage limit reached|1760500800")
    assert is_quota_error("deepseek returned 402: Insufficient Balance")
    assert is_quota_error("You've hit your usage limit. Try again in 2 hours 13 minutes.")
    assert not is_quota_error("claude failed: unknown option --foo")

    epoch = int((NOW + timedelta(hours=3)).timestamp())
    assert reset_time(f"Claude AI usage limit reached|{epoch}", NOW) == NOW + timedelta(hours=3)
    assert reset_time("hit your usage limit, try again in 2 hours 13 minutes", NOW) == NOW + timedelta(hours=2, minutes=13)
    assert reset_time("openai returned 429: rate limit", NOW) == NOW + timedelta(minutes=5)
    assert reset_time("deepseek returned 402: Insufficient Balance", NOW) == NOW + timedelta(hours=1)
    resets = reset_time("You've hit your limit · resets 3pm", NOW).astimezone()
    assert (resets.hour, resets.minute) == (15, 0) and NOW < resets <= NOW + timedelta(days=1)


async def test_the_registry_marks_a_model_out_of_quota_and_leaves_it_out_of_routing(monkeypatch):
    registry = ProviderRegistry()
    monkeypatch.setattr(registry.cli, "available_model_ids", lambda: {"cli:claude"})
    calls = []

    async def complete(model_id, prompt, *args, **kwargs):
        calls.append(model_id)
        raise RuntimeError("claude failed: Claude AI usage limit reached")

    monkeypatch.setattr(registry, "_complete", complete)
    with pytest.raises(QuotaExhausted) as caught:
        await registry.complete("cli:claude", "hi")
    assert caught.value.model_id == "cli:claude" and "额度用完" in str(caught.value)
    assert "cli:claude" not in registry.configured_models([])
    # Asked again, it does not even try until the reset.
    with pytest.raises(QuotaExhausted):
        await registry.complete("cli:claude", "hi")
    assert calls == ["cli:claude"]
    [entry] = registry.quota.public()
    assert entry["model_id"] == "cli:claude"


def model(model_id: str, coding: float) -> ModelProfile:
    return ModelProfile(id=model_id, provider=model_id.split(":")[0], display_name=model_id, channel="api",
                        context_window=1_000_000, input_per_million=1, output_per_million=1,
                        execution_priority=100, supports_tools=False,
                        scores=CapabilityScore(coding=coding, planning=80, reliability=90))


MODELS = {m.id: m for m in [model("x:cheap", 80), model("y:backup", 85)]}


class Providers:
    """Models in ``empty`` are out of quota; the others do the work."""

    def __init__(self, empty: set[str]):
        self.quota, self.empty, self.calls = QuotaTracker(), empty, []

    def configured_models(self, ids):
        return {model_id for model_id in ids if not self.quota.exhausted(model_id)}

    async def complete(self, model_id, prompt, workspace=None, **_kwargs):
        self.calls.append(model_id)
        if model_id in self.empty:
            raise self.quota.mark(model_id, f"{model_id} returned 402: Insufficient Balance")
        return ProviderResult("done", 10, 10, model_id)


async def run_one(tmp_path, monkeypatch, providers, model_id="x:cheap", model_lookup=None):
    monkeypatch.setattr(settings, "workspace_root", tmp_path / "workspaces")
    monkeypatch.setattr(settings, "review_nodes", False)
    database = Database(tmp_path / "test.db")
    node = TaskNode(id="n", title="Build", instructions="build it", capability="coding", model_id=model_id)
    database.save_run(ExecutionRun(id="r", prompt="p", status=RunStatus.QUEUED, budget_usd=1,
                                   plan=TaskPlan(summary="s", nodes=[node])))
    await Executor(database, providers, model_lookup or MODELS).execute("r")
    return database, database.get_run("r")


async def test_a_step_whose_model_runs_out_of_quota_moves_to_a_backup(tmp_path, monkeypatch):
    database, run = await run_one(tmp_path, monkeypatch, Providers({"x:cheap"}))

    node = run.plan.nodes[0]
    assert run.status == RunStatus.SUCCEEDED and node.model_id == "y:backup"
    assert [(a.model_id, a.outcome) for a in node.attempts] == [("x:cheap", "quota"), ("y:backup", "passed")]
    assert node.failed_attempts == 0
    [switch] = [e for e in database.events("r") if e.kind == "node.model_switched"]
    assert (switch.data["model_id"], switch.data["next_model_id"]) == ("x:cheap", "y:backup")


async def test_a_step_routed_to_a_model_already_out_of_quota_switches_before_calling_it(tmp_path, monkeypatch):
    providers = Providers(set())
    providers.quota.mark("x:cheap", "usage limit reached")
    _database, run = await run_one(tmp_path, monkeypatch, providers)

    assert run.status == RunStatus.SUCCEEDED and providers.calls == ["y:backup"]


async def test_with_every_model_out_of_quota_the_task_pauses_until_one_is_back(tmp_path, monkeypatch):
    database, run = await run_one(tmp_path, monkeypatch, Providers({"x:cheap", "y:backup"}))

    node = run.plan.nodes[0]
    assert run.status == RunStatus.PAUSED and node.status == NodeStatus.PENDING
    assert "没有其它可用模型能接替" in run.error and node.failed_attempts == 0
    event = database.events("r")[-1]
    assert (event.kind, event.data["reason"]) == ("approval.required", "quota_exhausted")


async def test_switching_drops_obsolete_wait_advice_but_keeps_verification_feedback(tmp_path):
    providers = Providers(set())
    database = Database(tmp_path / "test.db")
    advice = "Wait for the old model to recover before doing any work"
    node = TaskNode(id="n", title="Build", instructions="build", capability="coding", model_id="x:cheap",
                    status=NodeStatus.RUNNING, feedback=f"Fix the verified mismatch\n诊断建议：{advice}")
    database.save_run(ExecutionRun(id="r", prompt="p", status=RunStatus.RUNNING, budget_usd=1,
                                   plan=TaskPlan(summary="s", nodes=[node]), diagnoses=[
                                       Diagnosis(id="d", trigger="auto", node_id="n", status="done",
                                                 verdict="blocked", report=SESSION_LIMIT, advice=advice),
                                   ]))
    exc = providers.quota.mark("x:cheap", "usage limit reached")
    await Executor(database, providers, MODELS)._switch_model("r", "n", exc)
    switched = database.get_run("r").plan.nodes[0]
    assert switched.model_id == "y:backup" and switched.feedback == "Fix the verified mismatch"


async def test_claude_session_limit_hands_the_original_work_to_codex(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "workspace_root", tmp_path / "workspaces")
    monkeypatch.setattr(settings, "review_nodes", False)
    monkeypatch.setattr(settings, "auto_diagnose", True)
    registry = ProviderRegistry()
    monkeypatch.setattr(registry.cli, "available_model_ids", lambda: {"cli:claude", "cli:codex"})
    calls = []

    async def complete(model_id, prompt, workspace, **kwargs):
        calls.append((model_id, prompt))
        if model_id == "cli:claude":
            raise RuntimeError(SESSION_LIMIT)
        return "continued the original work"

    monkeypatch.setattr(registry.cli, "complete", complete)
    models = {
        mid: model(mid, score).model_copy(update={
            "channel": "cli", "input_per_million": 0, "output_per_million": 0,
        })
        for mid, score in [("cli:claude", 96.7), ("cli:codex", 94.6)]
    }
    node = TaskNode(id="work", title="Cross-check", instructions="Compare the collected evidence",
                    capability="coding", quality_threshold=90, model_id="cli:claude",
                    dependencies=["done"], feedback="Keep the existing verification feedback")
    completed = TaskNode(id="done", title="Collect", instructions="Collect evidence", capability="research",
                         status=NodeStatus.SUCCEEDED, result="Existing evidence")
    database = Database(tmp_path / "test.db")
    database.save_run(ExecutionRun(id="r", prompt="p", status=RunStatus.QUEUED, budget_usd=1,
                                   plan=TaskPlan(summary="s", nodes=[completed, node])))
    try:
        await Executor(database, registry, models).execute("r")
    finally:
        await registry.client.aclose()

    run = database.get_run("r")
    assert run.status == RunStatus.SUCCEEDED and run.diagnoses == []
    assert [mid for mid, _ in calls] == ["cli:claude", "cli:codex"]
    assert calls[0][1] == calls[1][1]
    assert "Keep the existing verification feedback" in calls[1][1]
    finished, handed_off = run.plan.nodes
    assert finished.result == "Existing evidence" and finished.attempts == []
    assert [(a.model_id, a.outcome) for a in handed_off.attempts] == [
        ("cli:claude", "quota"), ("cli:codex", "passed"),
    ]
    assert handed_off.failed_attempts == 0
    assert registry.quota.exhausted("cli:claude")
    assert "cli:claude" not in registry.configured_models(list(models))
    assert [e.kind for e in database.events("r")].count("node.model_switched") == 1


async def test_an_upstream_503_switches_models_and_other_projects_skip_the_service(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setenv("MINIMAX_API_KEY", "test-key")
    registry = ProviderRegistry()
    registry.retry_base_delay = 0
    calls = []
    import httpx

    def response(request):
        calls.append(request.url.host)
        if request.url.host == "api.deepseek.com":
            return httpx.Response(503, text="Service is too busy")
        return httpx.Response(200, json={"choices": [{"message": {"content": "done"}}]})

    await registry.client.aclose()
    registry.client = httpx.AsyncClient(transport=httpx.MockTransport(response))
    models = {m.id: m for m in [model("deepseek:deepseek-flash", 80), model("minimax:MiniMax-M3", 85)]}
    try:
        database, run = await run_one(tmp_path, monkeypatch, registry, "deepseek:deepseek-flash", models)
        assert run.status == RunStatus.SUCCEEDED
        assert [(a.model_id, a.outcome) for a in run.plan.nodes[0].attempts] == [
            ("deepseek:deepseek-flash", "unavailable"), ("minimax:MiniMax-M3", "passed"),
        ]
        assert len(calls) == registry.max_attempts + 1
        assert "deepseek:deepseek-flash" not in registry.configured_models(list(models))
        with pytest.raises(ModelUnavailable):
            await registry.complete("deepseek:deepseek-flash", "another project's task")
        assert len(calls) == registry.max_attempts + 1
        assert next(e for e in database.events("r") if e.kind == "node.model_switched").data["reason"] == "model_unavailable"
    finally:
        await registry.client.aclose()


def test_cooldowns_and_model_scoped_error_rules_survive_a_restart(tmp_path):
    database = Database(tmp_path / "shared.db")
    tracker = QuotaTracker(database)
    message = "provider denied request: E_WINDOW_EMPTY request_id=abcd-1234"
    tracker.learn("x:one", message, "quota", "run1", "d1")
    marked = tracker.mark("x:one", "usage limit reached")
    tracker.mark("y:two", "y returned 503", "unavailable")
    restarted = QuotaTracker(database)
    assert restarted.error("x:one").until == marked.until
    assert restarted.error("y:two").kind == "unavailable"
    assert restarted.classify("x:one", message.replace("abcd-1234", "new-id")) == "quota"
    assert restarted.classify("x:other", message) is None
    assert restarted.classify("x:one", "provider denied request: BAD_PARAMETER") is None
    restarted.clear("x:one")
    assert not QuotaTracker(database).exhausted("x:one")
    # Clearing a cooldown does not discard the learned fix for the next occurrence.
    assert QuotaTracker(database).classify("x:one", message) == "quota"
    assert QuotaTracker(database).rules[0]["diagnosis_id"] == "d1"


async def test_saving_a_new_key_puts_its_models_back_in_routing(task_api, monkeypatch):
    api, client = task_api
    api.providers.quota.mark("deepseek:deepseek-flash", "deepseek returned 402: Insufficient Balance")
    monkeypatch.setattr(api, "set_provider_key", lambda provider, key: "session")
    assert (await client.get("/api/models/quota")).json()[0]["model_id"] == "deepseek:deepseek-flash"
    assert (await client.put("/api/providers/deepseek/key", json={"api_key": "sk-new-key-123"}, headers=ORIGIN)).status_code == 200
    assert (await client.get("/api/models/quota")).json() == []
