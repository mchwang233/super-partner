import asyncio
import json

import httpx
import pytest
from super_partner.config import settings
from super_partner.db import Database
from super_partner.models import CapabilityScore, ModelProfile
from super_partner.providers import Conversation, ProviderRegistry
from super_partner.routing import pick_reviewer


def model(name, score, channel="api", price=1):
    return ModelProfile(id=name, provider=name.split(":")[0], display_name=name, channel=channel,
                        context_window=200_000, input_per_million=price, output_per_million=price,
                        scores=CapabilityScore(**{key: score for key in CapabilityScore.model_fields}))


def test_review_quality_is_a_gate_before_api_preference_and_independence():
    strong, weak = model("cli:codex", 96, "cli", 0), model("api:cheap", 89)
    models = [strong, weak]
    assert pick_reviewer(models, {m.id for m in models}, strong.id, 100).id == strong.id
    assert pick_reviewer(models, {weak.id}, strong.id, 100) is None


def test_conversation_checkpoint_survives_restart_and_deletion(tmp_path):
    from super_partner.models import ExecutionRun, TaskPlan
    path = tmp_path / "state.db"
    db = Database(path)
    run = ExecutionRun(id="r", prompt="test", budget_usd=1, status="cancelled", plan=TaskPlan(summary="x", nodes=[]))
    db.save_run(run)
    db.save_conversation("r", "n", {"model_id": "api:x", "state": {"history": [{"role": "user", "content": "constraint"}]}})
    db.connection.close()
    reopened = Database(path)
    assert reopened.conversation("r", "n")["state"]["history"][0]["content"] == "constraint"
    reopened.delete_run("r")
    assert reopened.conversation("r", "n") is None


async def test_rejected_effort_is_visible_on_every_affected_request(tmp_path, monkeypatch):
    monkeypatch.setenv("ZHIPU_API_KEY", "test")
    from test_providers import deepseek
    replies = iter([httpx.Response(400, text="unsupported reasoning_effort"),
                    httpx.Response(200, json=deepseek({"content": "ok"}, "stop")),
                    httpx.Response(200, json=deepseek({"content": "ok"}, "stop"))])
    registry = ProviderRegistry(Database(tmp_path / "calls.db"))
    registry.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: next(replies)))
    try:
        first = await registry.complete("glm:test", "test", effort="max")
        second = await registry.complete("glm:test", "test", effort="max")
        assert first.warnings and second.warnings
        calls = list(reversed(registry.db.model_calls()))
        assert len(calls) == 2
        assert calls[0]["requested_effort"] == "max"
        assert calls[0]["effective_effort"] == "provider_default"
        assert calls[0]["requests"] == 2
        assert calls[1]["warnings"]
    finally:
        await registry.client.aclose()


def test_interrupted_tool_completion_is_unknown_not_claimed_unrun():
    from super_partner.providers import _reopen
    state = {"history": [{"role": "assistant", "tool_calls": [{}]}], "calls": [("a", "Bash", {"command": "change"})]}
    _reopen(state, "continue")
    assert "unknown" in state["results"][0][2].lower()


def test_invalid_progress_verdict_cannot_silently_continue():
    from super_partner.progress import parse
    assert parse("I cannot decide") is not None
    assert parse("VERDICT: CONTINUE") is None


def saved_run(db):
    from super_partner.models import ExecutionRun, TaskNode, TaskPlan
    node = TaskNode(id="n", title="Build", instructions="keep the public API", capability="coding", model_id="deepseek:test")
    run = ExecutionRun(id="r", prompt="build", status="running", budget_usd=10, plan=TaskPlan(summary="build", nodes=[node]))
    db.save_run(run)
    return run, node


def test_executor_restores_an_inflight_checkpoint_and_invalidates_new_generation(tmp_path):
    from super_partner.executor import Executor
    db = Database(tmp_path / "state.db")
    run, node = saved_run(db)
    first = Executor(db, None, [])
    conversation = first._conversation(run, node)
    conversation.model_id = node.model_id
    conversation.state = {"history": [{"role": "user", "content": "a critical decision"}]}
    conversation.save()
    restarted = Executor(db, None, [])
    restored = restarted._conversation(run, node)
    assert "critical decision" in restored.state["history"][0]["content"]
    assert "restarted" in restored.followup
    node.generation += 1
    assert restarted._conversation(run, node).state is None


def test_model_switch_handoff_and_recall_are_scoped(tmp_path):
    from super_partner.conversation import recall
    from super_partner.executor import Executor
    db = Database(tmp_path / "state.db")
    run, node = saved_run(db)
    executor = Executor(db, None, [])
    original = executor._conversation(run, node)
    original.model_id = node.model_id
    original.state = {"history": [{"role": "assistant", "content": "Do not repeat failed check X"}]}
    original.save()
    node.model_id = "anthropic:other"
    following = executor._conversation(run, node)
    assert "failed check X" in following.handoff and "RecallHistory" in following.handoff
    archive_id = int(following.handoff.rsplit("archive_id=", 1)[1].rstrip("."))
    assert recall(following, {"archive_id": archive_id})[1]
    assert db.conversation_archive(run.id, "other-node", archive_id) is None


@pytest.mark.parametrize("provider", ["deepseek", "anthropic", "google"])
async def test_native_protocol_restores_after_a_tool_was_interrupted(tmp_path, monkeypatch, provider):
    from super_partner.providers import NOT_RUN
    from super_partner.secrets import PROVIDER_ENV
    monkeypatch.setenv(PROVIDER_ENV[provider], "test")
    from test_providers import anthropic_reply, deepseek
    state = {"provider": provider, "calls": [("c", "Bash", {"command": "do once"})], "results": None}
    if provider == "anthropic":
        state["history"] = [{"role": "user", "content": "work"}, {"role": "assistant", "content": [
            {"type": "tool_use", "id": "c", "name": "Bash", "input": {"command": "do once"}}]}]
        reply = anthropic_reply([{"type": "text", "text": "done"}])
    elif provider == "google":
        state["history"] = [{"role": "user", "parts": [{"text": "work"}]}, {"role": "model", "parts": [
            {"functionCall": {"name": "Bash", "args": {"command": "do once"}}}]}]
        reply = {"candidates": [{"content": {"parts": [{"text": "done"}]}}], "usageMetadata": {}}
    else:
        state["history"] = [{"role": "user", "content": "work"}, {"role": "assistant", "tool_calls": [
            {"id": "c", "type": "function", "function": {"name": "Bash", "arguments": "{}"}}]}]
        reply = deepseek({"content": "done"}, "stop")
    from test_providers import registry_with
    sent = []
    registry = registry_with([reply], sent)
    conversation = Conversation(model_id=f"{provider}:test", state=state, followup="resume safely")
    try:
        result = await registry.complete(conversation.model_id, "work", tmp_path, act=True, conversation=conversation)
    finally:
        await registry.client.aclose()
    assert result.text == "done"
    assert NOT_RUN in json.dumps(sent[0])
    assert "resume safely" in json.dumps(sent[0])


async def test_structured_compaction_keeps_constraints_and_original_history(tmp_path):
    from super_partner.conversation import SUMMARY_KEYS
    db = Database(tmp_path / "state.db")
    saved_run(db)
    registry = ProviderRegistry(db, {"x:test": model("x:test", 90).model_copy(update={"context_window": 10000})})
    state = {"provider": "openai", "history": [{"role": "user", "content": "Keep API unchanged"},
             {"role": "tool", "content": "evidence " * 4000}]}
    conversation = Conversation(model_id="x:test", state=state,
                                archive=lambda body: db.archive_conversation("r", "n", body))
    async def step(summary_state):
        assert "evidence" in summary_state["prompt"]
        return json.dumps({key: ["important fact"] for key in SUMMARY_KEYS}), [], 100, 20, False
    try:
        used = await registry._compact_context("x:test", state, step, None, conversation)
        assert used == (100, 20, 0)
        assert state["history"][0]["content"] == "Keep API unchanged"
        assert "RecallHistory archive_id=" in json.dumps(state["history"])
        assert len(db.conversation_archive("r", "n", 1)["state"]["history"][1]["content"]) > 10000
        # Invalid compaction leaves the original history intact.
        state["history"].append({"role": "tool", "content": "original evidence" * 3000})
        before = json.dumps(state["history"])
        async def invalid(_):
            return '{"goals": []}', [], 10, 2, False
        from super_partner.budget import ProviderError
        with pytest.raises(ProviderError, match="结构验证"):
            await registry._compact_context("x:test", state, invalid, None, conversation)
        assert json.dumps(state["history"]) == before
    finally:
        await registry.client.aclose()


async def test_summary_cannot_spend_the_last_hard_turn(tmp_path):
    from super_partner.budget import StepLimitExceeded
    registry = ProviderRegistry(models={"x:test": model("x:test", 90).model_copy(update={"context_window": 1000})})
    state = {"history": [{"role": "tool", "content": "x" * 10000}]}
    conversation = Conversation(state=state, archive=lambda _: 1)
    class Budget:
        def turn_remaining(self):
            return 1
    try:
        with pytest.raises(StepLimitExceeded):
            await registry._compact_context("x:test", state, None, Budget(), conversation)
    finally:
        await registry.client.aclose()


async def test_provider_gate_serializes_and_cancellation_releases_its_slot():
    from super_partner.call_control import ProviderGate
    gate = ProviderGate()
    entered, release = asyncio.Event(), asyncio.Event()
    async def first():
        async with gate.hold({"concurrency": 1}, 10):
            entered.set()
            await release.wait()
    task = asyncio.create_task(first())
    await entered.wait()
    second_entered = asyncio.Event()
    async def second():
        async with gate.hold({"concurrency": 1}, 10):
            second_entered.set()
    waiting = asyncio.create_task(second())
    await asyncio.sleep(0)
    assert not second_entered.is_set()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(waiting, .5)
    assert gate.active == 0


async def test_rpm_and_tpm_wait_and_reject_impossible_requests(monkeypatch):
    from super_partner.budget import ProviderError
    from super_partner.call_control import ProviderGate
    gate = ProviderGate()
    async with gate.hold({"rpm": 1, "tpm": 100}, 80):
        pass
    async def wait_for_admission():
        async with gate.hold({"rpm": 1, "tpm": 100}, 30):
            pytest.fail("rate gate admitted too early")
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(wait_for_admission(), .02)
    with pytest.raises(ProviderError, match="TPM"):
        async with gate.hold({"tpm": 100}, 101):
            pytest.fail("impossible request admitted")
    assert gate.active == 0


def test_cli_usage_and_missing_usage_are_distinguished():
    from super_partner.cli_channels import cli_usage, codex_output
    from super_partner.providers import _cli_result
    result = _cli_result(codex_output('\n'.join([
        json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "done"}}),
        json.dumps({"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 70, "output_tokens": 20}}),
    ])), "cli:codex")
    assert (result.text, result.input_tokens, result.cached_input_tokens, result.usage_known) == ("done", 100, 70, True)
    assert not _cli_result("plain output", "cli:other").usage_known
    assert cli_usage({"usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 20,
                                "cache_creation_input_tokens": 30}})["input_tokens"] == 60


async def test_extension_and_hooks_use_workspace_permissions_and_quote_json(tmp_path, monkeypatch):
    import shlex

    from super_partner.agent import Workspace
    from super_partner.extensions import definitions
    monkeypatch.setattr(settings, "api_tool_extensions", {"Check": {"command": ["python", "check.py"],
                        "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}}})
    monkeypatch.setattr(settings, "api_tool_hooks", {"before_tool": [["python", "guard.py"]]})
    ws = Workspace(tmp_path, network=False)
    calls = []
    async def bash(command, timeout):
        calls.append(shlex.split(command))
        return "ok", True
    monkeypatch.setattr(ws, "bash", bash)
    try:
        assert definitions(ws)[0]["name"] == "Check"
        assert (await ws.run("Check", {"name": "$(do-not-execute);`bad`"}))[1]
        assert json.loads(calls[-1][-1])["name"] == "$(do-not-execute);`bad`"
        assert not ws.network and calls[0][1] == "guard.py"
        async def deny(*_):
            return "denied", False
        monkeypatch.setattr(ws, "bash", deny)
        assert "not started" in (await ws.run("Write", {"file_path": "x", "content": "bad"}))[0]
        assert not (tmp_path / "x").exists()
    finally:
        ws.close()
    review = Workspace(tmp_path, read_only=True)
    try:
        assert definitions(review) == []
        assert not (await review.run("Check", {"name": "x"}))[1]
    finally:
        review.close()


def test_route_feedback_has_a_sample_floor_and_penalizes_rework(monkeypatch):
    from super_partner.models import TaskNode
    from super_partner.routing import route_node
    monkeypatch.setattr(settings, "api_agent", False)
    cheap, reliable = model("x:cheap", 90, price=.8), model("x:reliable", 90, price=1)
    node = TaskNode(id="n", title="t", instructions="i", capability="coding", difficulty="normal")
    stats = {("x:cheap", "coding", "normal"): {"samples": 7, "successes": 0, "cost_usd": .1}}
    route_node(node, [cheap, reliable], {cheap.id, reliable.id}, feedback=stats)
    assert node.model_id == cheap.id
    stats["x:cheap", "coding", "normal"]["samples"] = 8
    route_node(node, [cheap, reliable], {cheap.id, reliable.id}, feedback=stats)
    assert node.model_id == reliable.id


async def test_calls_api_is_task_scoped_and_marks_unknown_usage(task_api):
    api, client = task_api
    saved_run(api.db)
    api.db.save_model_call({"id": "c", "run_id": "r", "node_id": "n", "started_at": "now",
                           "requests": 1, "usage_known": False, "cost_usd": 0, "input_tokens": 0,
                           "output_tokens": 0, "cached_input_tokens": 0, "retries": 0})
    response = await client.get("/api/tasks/r/calls")
    assert response.status_code == 200
    assert response.json()["summary"]["unknown_usage"] == 1
    assert (await client.get("/api/tasks/missing/calls")).status_code == 404


async def test_failed_declared_check_blocks_a_claimed_success(tmp_path, monkeypatch):
    from super_partner.agent import Workspace
    from super_partner.executor import Executor
    db = Database(tmp_path / "state.db")
    run, node = saved_run(db)
    node.check = "test -f required-output"
    calls = []
    async def execute(self, name, args):
        calls.append((name, args))
        return "required-output missing", False
    monkeypatch.setattr(Workspace, "run", execute)
    executor = Executor(db, None, {})
    verdict = await executor._check(run, node, "All tests passed, done!", [], tmp_path)
    assert not verdict.passed and not verdict.verified and "验收命令失败" in verdict.reason
    assert calls == [("Bash", {"command": node.check})]
    assert executor.tool_logs[run.id, node.id][-1]["ok"] is False


async def test_strict_effort_mode_never_retries_without_reasoning(tmp_path, monkeypatch):
    from super_partner.budget import ProviderError
    from test_providers import registry_with
    monkeypatch.setenv("ZHIPU_API_KEY", "test")
    monkeypatch.setattr(settings, "reasoning_fallback", "strict")
    sent = []
    registry = registry_with([httpx.Response(400, text="unsupported reasoning_effort")], sent)
    try:
        with pytest.raises(ProviderError, match="禁止自动降级"):
            await registry.complete("glm:test", "work", effort="max")
        assert len(sent) == 1 and sent[0]["reasoning_effort"] == "max"
    finally:
        await registry.client.aclose()


def test_graceful_interruption_restores_without_replaying_completed_tools(tmp_path):
    from super_partner.conversation import reopen
    from super_partner.executor import Executor
    from super_partner.models import NodeAttempt
    db = Database(tmp_path / "state.db")
    run, node = saved_run(db)
    executor = Executor(db, None, {})
    held = executor._conversation(run, node)
    held.model_id = node.model_id
    held.state = {"history": [{"role": "user", "content": "work"}], "calls": [
        ("a", "Write", {}), ("b", "Bash", {})], "results": [("a", "Write", "written")], "provider": "openai"}
    held.save()
    node.attempts.append(NodeAttempt(model_id=node.model_id, outcome="cancelled", interrupted=True))
    restored = Executor(db, None, {})._conversation(run, node)
    assert restored.followup and restored.state["calls"]
    reopen(restored.state, "resume")
    assert restored.state["results"][0] == ("a", "Write", "written") or restored.state["results"][0] == ["a", "Write", "written"]
    assert "unknown" in restored.state["results"][1][2].lower()


def test_restart_marks_unfinished_usage_unknown(tmp_path):
    db = Database(tmp_path / "state.db")
    db.save_model_call({"id": "c", "run_id": None, "started_at": "now", "status": "running", "usage_known": True})
    db.interrupt_model_calls()
    [record] = db.model_calls()
    assert record["status"] == "interrupted" and not record["usage_known"]


def test_provider_config_rejects_negative_limits_and_misspelled_hooks():
    from pydantic import ValidationError
    from super_partner.config import Settings
    with pytest.raises(ValidationError):
        Settings(provider_limits={"x": {"concurrency": -1}})
    with pytest.raises(ValidationError):
        Settings(api_tool_hooks={"typo": [["check"]]})
