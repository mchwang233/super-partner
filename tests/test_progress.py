"""No turn limit and no fixed timeout: a long attempt is checked, and stopped only if stuck."""
import asyncio
import json
import os

import httpx
import pytest
from super_partner import progress
from super_partner.budget import ProviderError
from super_partner.cli_channels import CliChannelRegistry
from super_partner.config import settings
from super_partner.db import Database
from super_partner.executor import Executor
from super_partner.models import TaskNode
from super_partner.providers import ProviderRegistry, ProviderResult
from super_partner.recovery import turns_for
from test_diagnosis import MODELS, one_node_run
from test_executor import process_alive

READ = {"id": "c", "type": "function", "function": {"name": "list_dir", "arguments": json.dumps({"path": "."})}}


def reply(message: dict) -> dict:
    return {"choices": [{"message": message, "finish_reason": "stop"}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}}


@pytest.fixture
def often(monkeypatch):
    """Check every 0.2 s instead of every 10 minutes."""
    monkeypatch.setattr(settings, "progress_check_minutes", 0.2 / 60)


def test_the_checker_must_say_stop_to_stop():
    assert progress.parse("还在写测试，进展正常。\nVERDICT: CONTINUE") is None
    assert progress.parse("VERDICT: STOP: 连续 12 次运行同一条失败的命令") == "连续 12 次运行同一条失败的命令"
    assert progress.parse("看不出来") is None


def test_there_is_no_turn_limit_by_default():
    assert settings.api_agent_max_turns == 0
    assert turns_for(TaskNode(id="n", title="t", instructions="i", capability="coding")) is None


async def test_an_api_model_works_past_the_old_limit_while_checks_say_continue(tmp_path, monkeypatch, often):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test")
    turns, checks = 60, []

    def respond(request):
        count = len(json.loads(request.content)["messages"])
        done = count > 2 * turns  # each turn adds the call and its result
        return httpx.Response(200, json=reply({"content": "done"} if done else {"content": None, "tool_calls": [READ]}))

    async def watch(evidence):
        checks.append(evidence)

    registry = ProviderRegistry()
    registry.client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    slow = registry._post

    async def post(*args, **kwargs):
        await asyncio.sleep(0.01)
        return await slow(*args, **kwargs)

    registry._post = post
    try:
        result = await registry.complete("deepseek:deepseek-flash", "work", tmp_path, act=True, watch=watch)
    finally:
        await registry.client.aclose()
    assert result.text == "done" and result.turns > 40 and not result.turn_limit
    assert checks and "list_dir" in checks[-1]


async def test_an_api_model_the_check_finds_stuck_is_stopped(tmp_path, monkeypatch, often):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test")

    async def respond(request):
        await asyncio.sleep(0.05)
        return httpx.Response(200, json=reply({"content": None, "tool_calls": [READ]}))

    registry = ProviderRegistry()
    registry.client = httpx.AsyncClient(transport=httpx.MockTransport(respond))

    async def watch(evidence):
        return "一直在列同一个目录"

    try:
        with pytest.raises(ProviderError, match="一直在列同一个目录") as caught:
            await registry.complete("deepseek:deepseek-flash", "work", tmp_path, act=True, watch=watch)
    finally:
        await registry.client.aclose()
    assert progress.STOPPED in str(caught.value)


def fake_codex(tmp_path, monkeypatch, script: str):
    cli = tmp_path / "bin" / "codex"
    cli.parent.mkdir()
    cli.write_text(script)
    cli.chmod(0o755)
    monkeypatch.setenv("PATH", f"{cli.parent}:{os.environ['PATH']}")


async def test_a_cli_past_its_timeout_is_checked_not_killed(tmp_path, monkeypatch, often):
    fake_codex(tmp_path, monkeypatch, "#!/bin/sh\necho work > made.txt\nsleep 1\necho finished\n")
    checks = []

    async def watch(evidence):
        checks.append(evidence)

    text = await CliChannelRegistry().complete("cli:codex", "hi", tmp_path, timeout=0.1, watch=watch)
    assert text == "finished"
    assert checks and "made.txt" in checks[0] and "sleep" in checks[0]


async def test_a_stuck_cli_is_stopped_with_its_processes_and_does_not_fall_back(tmp_path, monkeypatch, often):
    child = tmp_path / "child.pid"
    fake_codex(tmp_path, monkeypatch, f"#!/bin/sh\nsleep 30 &\necho $! > {child}\nwait\n")

    async def watch(evidence):
        return "开发服务器一直在前台运行，没有任何进展"

    registry = ProviderRegistry()
    with pytest.raises(ProviderError, match="开发服务器一直在前台运行"):
        await registry.complete("cli:codex", "hi", tmp_path, watch=watch)
    for _ in range(200):
        if not process_alive(int(child.read_text())):
            break
        await asyncio.sleep(0.01)
    assert not process_alive(int(child.read_text()))


async def test_the_executor_asks_another_model_and_records_the_check(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "workspace_root", tmp_path / "workspaces")
    database = Database(tmp_path / "test.db")
    one_node_run(database)
    asked = []

    class Checker:
        def configured_models(self, ids):
            return set(ids)

        async def complete(self, model_id, prompt, workspace=None, **kwargs):
            asked.append((model_id, prompt))
            return ProviderResult("同一条命令失败了 9 次\nVERDICT: STOP: 反复运行同一条失败的命令", 0, 0, model_id)

    executor = Executor(database, Checker(), MODELS)
    run = database.get_run("r")
    watch = executor._progress_watch(run, run.plan.nodes[0], "x:api")
    reason = await watch("- turn 9: run_command node --check app.js  (failed)")
    assert reason == "反复运行同一条失败的命令"
    # Not the model at work, and it saw the step and the evidence.
    [(checker, prompt)] = asked
    assert checker == "cli:claude" and "run node --check" in prompt and "turn 9" in prompt
    event = [e for e in database.events("r") if e.kind == "node.progress_check"][-1]
    assert event.data["stop"] is True and event.data["reason"] == reason
