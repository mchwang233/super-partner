import json
from pathlib import Path

import httpx
import pytest
from super_partner import agent, environment
from super_partner.config import settings
from super_partner.db import Database
from super_partner.executor import Executor
from super_partner.models import (
    CapabilityScore,
    ExecutionRun,
    ModelProfile,
    NodeStatus,
    RunStatus,
    TaskNode,
    TaskPlan,
)
from super_partner.providers import ProviderError, ProviderRegistry

needs_bwrap = pytest.mark.skipif(not agent.sandbox_available(), reason="bubblewrap is not installed")


async def test_file_tools_stay_inside_the_working_directory(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    (tmp_path / "secret.txt").write_text("secret")
    (root / "link").symlink_to(tmp_path / "secret.txt")
    ws = agent.Workspace(root)

    assert await ws.call("write_file", {"path": "src/app.js", "content": "let a = 1;\n"}) == "Wrote src/app.js (11 bytes)"
    assert await ws.call("edit_file", {"path": "src/app.js", "old_text": "1", "new_text": "2"}) == "Edited src/app.js"
    assert await ws.call("read_file", {"path": "src/app.js"}) == "let a = 2;"
    assert (await ws.call("list_dir", {})).startswith("src/\n")
    for name, args in [
        ("read_file", {"path": "../secret.txt"}), ("read_file", {"path": "/etc/passwd"}),
        ("read_file", {"path": "link"}), ("write_file", {"path": "../escape.txt", "content": "x"}),
    ]:
        assert "outside the working directory" in await ws.call(name, args)
    assert not (tmp_path / "escape.txt").exists()
    assert "occurs 0 times" in await ws.call("edit_file", {"path": "src/app.js", "old_text": "zzz", "new_text": "y"})
    assert "JSON object" in await ws.call("write_file", {"__invalid__": "{oops"})
    assert "unknown tool" in await ws.call("rm_rf", {})


@needs_bwrap
@pytest.mark.parametrize("override_home", [False, True])
async def test_commands_run_sandboxed_without_keys_network_or_writes_outside(tmp_path, monkeypatch, override_home):
    import os
    import pwd

    if override_home:
        other_home = tmp_path / "temporary-home"
        other_home.mkdir()
        monkeypatch.setenv("HOME", str(other_home))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-leak")
    root = Path(settings.workspace_root).resolve() / "_agent_test"
    root.mkdir(parents=True, exist_ok=True)
    try:
        ws = agent.Workspace(root)
        args = ws.sandbox_args("true")
        assert str(Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()) in [
            args[index + 1] for index, value in enumerate(args[:-1]) if value == "--tmpfs"
        ]
        output = await ws.call("run_command", {"command": (
            "echo made > inside.txt; cat inside.txt; echo key=${OPENAI_API_KEY:-none}; "
            # Above the working directory is a throwaway in-memory home: writes never reach disk.
            "touch \"$(dirname \"$PWD\")/escape.txt\"; "
            "touch /usr/escape.txt 2>/dev/null && echo system-escaped || echo system-blocked; "
            "ls /home/*/.ssh 2>/dev/null | wc -l"
        )})
        assert output.startswith("exit code 0")
        assert "made" in output and "key=none" in output and "system-blocked" in output
        assert output.rstrip().endswith("0")
        assert (root / "inside.txt").read_text() == "made\n"
        assert not (root.parent / "escape.txt").exists()
        assert "timed out" in await ws.call("run_command", {"command": "sleep 5", "timeout_seconds": 1})
    finally:
        import shutil
        shutil.rmtree(root, ignore_errors=True)



@needs_bwrap
@pytest.mark.skipif(not __import__("shutil").which("socat"), reason="socat is not installed")
async def test_only_listed_host_ports_reach_the_sandbox(monkeypatch):
    import asyncio

    async def greet(reader, writer):
        writer.write(b"hello-from-host\n")
        await writer.drain()
        writer.close()

    listed = await asyncio.start_server(greet, "127.0.0.1", 0)
    unlisted = await asyncio.start_server(greet, "127.0.0.1", 0)
    port, other = (server.sockets[0].getsockname()[1] for server in (listed, unlisted))
    monkeypatch.setattr(settings, "sandbox_host_ports", [port])
    monkeypatch.setattr(settings, "sandbox_env", ["SP_TOOL_HOME"])
    monkeypatch.setenv("SP_TOOL_HOME", "/opt/tool")
    root = Path(settings.workspace_root).resolve() / "_agent_ports_test"
    root.mkdir(parents=True, exist_ok=True)
    try:
        output = await agent.Workspace(root).call("run_command", {"command": (
            f"timeout 5 bash -c 'exec 3<>/dev/tcp/127.0.0.1/{port}; cat <&3' || echo listed-failed; "
            f"timeout 2 bash -c 'exec 3<>/dev/tcp/127.0.0.1/{other}' 2>/dev/null && echo unlisted-open || echo unlisted-blocked; "
            "echo tool=$SP_TOOL_HOME"
        )})
        assert "hello-from-host" in output and "unlisted-blocked" in output and "tool=/opt/tool" in output
        assert f"local ports {port}" in agent.agent_instructions()
    finally:
        listed.close()
        unlisted.close()
        import shutil
        shutil.rmtree(root, ignore_errors=True)



@needs_bwrap
@pytest.mark.skipif(not agent.network_available(), reason="pasta is not installed")
async def test_a_task_allowing_the_network_reaches_out_but_not_local_services(monkeypatch, network_enabled):
    import asyncio

    async def greet(reader, writer):
        writer.write(b"hello-from-proxy\n")
        await writer.drain()
        writer.close()

    proxy = await asyncio.start_server(greet, "127.0.0.1", 0)
    service = await asyncio.start_server(greet, "127.0.0.1", 0)
    port, other = (server.sockets[0].getsockname()[1] for server in (proxy, service))
    for name in agent.PROXY_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", f"http://127.0.0.1:{port}")
    root = Path(settings.workspace_root).resolve() / "_agent_network_test"
    root.mkdir(parents=True, exist_ok=True)
    try:
        ws = agent.Workspace(root, network=True)
        assert agent.proxy_ports() == [port] and "--unshare-net" not in ws.sandbox_args("true")
        output = await ws.call("run_command", {"command": (
            # A route out exists, the user's local proxy is forwarded, other local ports are not.
            "awk '$2 == \"00000000\" {f = 1} END {exit !f}' /proc/net/route && echo routed || echo no-route; "
            f"timeout 5 bash -c 'exec 3<>/dev/tcp/127.0.0.1/{port}; cat <&3' || echo proxy-failed; "
            f"timeout 2 bash -c 'exec 3<>/dev/tcp/127.0.0.1/{other}' 2>/dev/null && echo local-open || echo local-blocked; "
            "echo proxy=$HTTPS_PROXY"
        )})
        assert "\nrouted" in output and "hello-from-proxy" in output and "local-blocked" in output
        assert f"proxy=http://127.0.0.1:{port}" in output
        assert "allows the internet" in ws.instructions() and "no network" not in ws.instructions()
        # Without the task's permission there is no route out at all.
        output = await agent.Workspace(root).call("run_command", {"command": (
            "awk '$2 == \"00000000\" {f = 1} END {exit !f}' /proc/net/route && echo routed || echo no-route"
        )})
        assert "no-route" in output
    finally:
        proxy.close()
        service.close()
        import shutil
        shutil.rmtree(root, ignore_errors=True)


def test_cli_sandboxes_open_to_the_internet_only_for_a_task_allowing_it():
    from super_partner import cli_channels

    claude = list(cli_channels.CLI_SPECS["cli:claude"].invoke_args)
    opened = cli_channels._with_network("cli:claude", claude)
    settings_arg = json.loads(opened[opened.index("--settings") + 1])
    assert settings_arg["sandbox"]["network"]["allowedDomains"] == ["*"]
    assert "127.0.0.1" in settings_arg["sandbox"]["network"]["deniedDomains"]
    assert settings_arg["sandbox"]["allowUnsandboxedCommands"] is False
    codex = cli_channels._with_network("cli:codex", list(cli_channels.CLI_SPECS["cli:codex"].invoke_args))
    assert codex[:3] == ["exec", "-c", "sandbox_workspace_write.network_access=true"] and "workspace-write" in codex


@needs_bwrap
async def test_tools_from_environment_repairs_reach_the_sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    tools = environment.prepare()
    (tools / "bin" / "mytool").write_text("#!/bin/sh\necho tool-ran\n")
    (tools / "bin" / "mytool").chmod(0o755)
    (tools / "python" / "mymod.py").write_text("print('module-ran')\n")
    (tools / "sandbox.json").write_text(json.dumps({"env": {"EDA_HOME": "/opt/eda", "PATH": "/evil", "bad name": "x"}}))
    root = Path(settings.workspace_root).resolve() / "_agent_tools_test"
    root.mkdir(parents=True, exist_ok=True)
    try:
        output = await agent.Workspace(root).call("run_command", {"command": (
            "mytool; python3 -c 'import mymod'; echo eda=$EDA_HOME; "
            f"touch {tools}/bin/x 2>/dev/null && echo tools-writable || echo tools-readonly"
        )})
        assert "tool-ran" in output and "module-ran" in output and "eda=/opt/eda" in output
        assert "tools-readonly" in output
    finally:
        import shutil
        shutil.rmtree(root, ignore_errors=True)


@needs_bwrap
async def test_host_mode_commands_leave_the_sandbox_but_cannot_use_sudo(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path / "data")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-leak")
    root = tmp_path / "work"
    root.mkdir()
    output = await agent.Workspace(root, host=True).call("run_command", {"command": (
        f"echo made > {tmp_path}/outside.txt; echo key=${{OPENAI_API_KEY:-none}}; "
        "sudo -n true 2>/dev/null && echo became-root || echo sudo-blocked"
    )})
    assert "key=none" in output and "sudo-blocked" in output
    assert (tmp_path / "outside.txt").read_text() == "made\n"
    assert "without root" in agent.Workspace(root, host=True).instructions()


def registry_with(respond) -> ProviderRegistry:
    registry = ProviderRegistry()
    registry.client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    return registry


def scripted(replies: list[dict], sent: list[dict]):
    replies = iter(replies)

    def respond(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=next(replies))
    return respond


async def test_openai_compatible_models_call_tools_until_done(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    sent = []
    registry = registry_with(scripted([
        {"choices": [{"message": {"content": None, "tool_calls": [{"id": "c1", "type": "function", "function": {
            "name": "write_file", "arguments": json.dumps({"path": "hello.txt", "content": "hi"})}}]}}],
         "usage": {"prompt_tokens": 10, "completion_tokens": 5}},
        {"choices": [{"message": {"content": "Wrote hello.txt"}}], "usage": {"prompt_tokens": 20, "completion_tokens": 3}},
    ], sent))
    try:
        result = await registry.complete("openai:gpt-x", "Create hello.txt", tmp_path, act=True)
    finally:
        await registry.client.aclose()
    assert (tmp_path / "hello.txt").read_text() == "hi"
    assert (result.text, result.input_tokens, result.output_tokens) == ("Wrote hello.txt", 30, 8)
    assert {tool["function"]["name"] for tool in sent[0]["tools"]} >= {"read_file", "write_file", "edit_file", "list_dir"}
    assert sent[1]["messages"][-1] == {"role": "tool", "tool_call_id": "c1", "content": "Wrote hello.txt (2 bytes)"}


async def test_anthropic_and_gemini_models_call_tools_until_done(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    monkeypatch.setenv("GOOGLE_API_KEY", "test")
    (tmp_path / "notes.md").write_text("remember this")
    sent = []
    registry = registry_with(scripted([
        {"content": [{"type": "tool_use", "id": "t1", "name": "read_file", "input": {"path": "notes.md"}}],
         "usage": {"input_tokens": 5, "output_tokens": 2}},
        {"content": [{"type": "text", "text": "It says remember this"}], "usage": {"input_tokens": 7, "output_tokens": 4}},
        {"candidates": [{"content": {"role": "model", "parts": [{"functionCall": {"name": "list_dir", "args": {}}}]}}],
         "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 1}},
        {"candidates": [{"content": {"role": "model", "parts": [{"text": "Found notes.md"}]}}],
         "usageMetadata": {"promptTokenCount": 4, "candidatesTokenCount": 2}},
    ], sent))
    try:
        claude = await registry.complete("anthropic:claude-x", "Read the notes", tmp_path, act=True)
        gemini = await registry.complete("google:gemini-x", "List files", tmp_path, act=True)
    finally:
        await registry.client.aclose()
    assert claude.text == "It says remember this" and (claude.input_tokens, claude.output_tokens) == (12, 6)
    assert sent[1]["messages"][-1]["content"][0] == {"type": "tool_result", "tool_use_id": "t1", "content": "remember this"}
    assert sent[0]["tools"][0]["input_schema"]["type"] == "object"
    assert gemini.text == "Found notes.md"
    assert sent[3]["contents"][-1]["parts"][0]["functionResponse"]["name"] == "list_dir"
    assert "notes.md" in sent[3]["contents"][-1]["parts"][0]["functionResponse"]["response"]["result"]


async def test_tool_loops_stop_at_the_budget_and_the_turn_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setattr(settings, "api_agent_max_turns", 3)
    looping = {"choices": [{"message": {"content": None, "tool_calls": [{"id": "c", "type": "function", "function": {
        "name": "list_dir", "arguments": "{}"}}]}}], "usage": {"prompt_tokens": 100, "completion_tokens": 100}}
    registry = registry_with(lambda request: httpx.Response(200, json=looping))
    try:
        with pytest.raises(ProviderError, match="3 轮"):
            await registry.complete("openai:gpt-x", "loop", tmp_path, act=True)
        with pytest.raises(ProviderError, match="预算"):
            await registry.complete("openai:gpt-x", "loop", tmp_path, act=True, spend_ok=lambda *_: False)
        # Without act it is a single plain call, with no tools offered.
        plain = await registry.complete("openai:gpt-x", "loop", tmp_path)
    finally:
        await registry.client.aclose()
    assert plain.input_tokens == 100


async def test_an_api_model_does_project_work_through_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setattr(settings, "workspace_root", tmp_path / "workspaces")
    monkeypatch.setattr(settings, "review_nodes", False)
    project = tmp_path / "project"
    project.mkdir()
    registry = registry_with(scripted([
        {"choices": [{"message": {"content": None, "tool_calls": [{"id": "c1", "type": "function", "function": {
            "name": "write_file", "arguments": json.dumps({"path": "feature.py", "content": "print(1)\n"})}}]}}],
         "usage": {"prompt_tokens": 1000, "completion_tokens": 100}},
        {"choices": [{"message": {"content": "Added feature.py"}}], "usage": {"prompt_tokens": 1000, "completion_tokens": 50}},
    ], []))
    gpt = ModelProfile(id="openai:gpt-x", provider="openai", display_name="GPT X", context_window=200_000,
                       input_per_million=1, output_per_million=2, scores=CapabilityScore(coding=90))
    node = TaskNode(id="n", title="Add feature", instructions="add it", capability="coding",
                    model_id=gpt.id, needs_workspace=True)
    database = Database(tmp_path / "test.db")
    database.save_run(ExecutionRun(id="r", prompt="p", status=RunStatus.QUEUED, budget_usd=1,
                                   project_dir=str(project), plan=TaskPlan(summary="s", nodes=[node])))
    try:
        await Executor(database, registry, {gpt.id: gpt}).execute("r")
    finally:
        await registry.client.aclose()
    run = database.get_run("r")
    assert run.status == RunStatus.SUCCEEDED and run.plan.nodes[0].status == NodeStatus.SUCCEEDED
    assert (project / "feature.py").read_text() == "print(1)\n"
    assert run.plan.nodes[0].changed_files == ["feature.py"]
    assert run.actual_cost_usd == pytest.approx((2000 * 1 + 150 * 2) / 1_000_000)


def test_a_cli_failure_reports_the_reason_from_its_json_output():
    from super_partner.cli_channels import _failure_detail

    usage = json.dumps({"type": "result", "is_error": True, "result": "Claude AI usage limit reached"}).encode()
    assert _failure_detail(usage, b"") == "Claude AI usage limit reached"
    assert _failure_detail(b"plain words", b"") == "plain words"
    assert _failure_detail(b"ignored", b"bad flag\n") == "bad flag"
    assert _failure_detail(b"", b"") == "no output"


def tool_turn_response(text=None, call=True):
    message = {"content": text}
    if call:
        message["tool_calls"] = [{"id": "c1", "type": "function", "function": {"name": "list_dir", "arguments": "{}"}}]
    return httpx.Response(200, json={"choices": [{"message": message, "finish_reason": "tool_calls" if call else "stop"}],
                                    "usage": {"prompt_tokens": 10, "completion_tokens": 5}})


async def run_turns(tmp_path, monkeypatch, max_turns, answer_at):
    """A model that lists the directory every turn until request ``answer_at``."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    bodies = []

    def respond(request):
        bodies.append(json.loads(request.content))
        return tool_turn_response("进度：完成了一半", call=len(bodies) < answer_at)

    registry = registry_with(respond)
    log = []
    try:
        result = await registry.complete("deepseek:deepseek-chat", "do it", tmp_path, act=True,
                                         max_turns=max_turns, tool_log=log)
    finally:
        await registry.client.aclose()
    return result, bodies, log


async def test_out_of_turns_the_model_is_asked_for_a_progress_summary(tmp_path, monkeypatch):
    result, bodies, log = await run_turns(tmp_path, monkeypatch, max_turns=3, answer_at=4)
    assert (result.text, result.turns, result.turn_limit) == ("进度：完成了一半", 3, True)
    assert len(bodies) == 4 and [call["turn"] for call in log] == [1, 2, 3]
    assert log[0] == {"turn": 1, "tool": "list_dir", "detail": ".", "ok": True}
    assert "You have at most 3 turns" in json.dumps(bodies[0], ensure_ascii=False)
    assert "No turns of tool calls are left" in json.dumps(bodies[3]["messages"][-1], ensure_ascii=False)


async def test_five_turns_before_the_limit_the_model_is_told_to_wrap_up(tmp_path, monkeypatch):
    result, bodies, _log = await run_turns(tmp_path, monkeypatch, max_turns=8, answer_at=5)
    assert not result.turn_limit and result.turns == 5
    assert "Only 5 turns of tool calls are left" in json.dumps(bodies[3]["messages"][-1], ensure_ascii=False)
    assert "Only 5 turns" not in json.dumps(bodies[2]["messages"][-1], ensure_ascii=False)


async def test_a_model_that_keeps_calling_tools_after_the_limit_fails_with_the_turn_limit(tmp_path, monkeypatch):
    with pytest.raises(ProviderError, match="在 2 轮工具调用后仍未完成"):
        await run_turns(tmp_path, monkeypatch, max_turns=2, answer_at=99)
