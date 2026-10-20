"""The tools API models act through are Claude Code's: Bash, Read, Write, Edit, MultiEdit, Glob,
Grep and LS, with its parameters and output."""
import asyncio
import os
import time

import pytest
from super_partner import agent
from super_partner.agent import Workspace


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "work"
    (root / "rtl").mkdir(parents=True)
    (root / "tb").mkdir()
    (root / "node_modules").mkdir()
    (root / "rtl" / "fifo.sv").write_text("module fifo;\n  logic full;\n  logic empty;\nendmodule\n")
    (root / "rtl" / "sync.sv").write_text("module sync;\nendmodule\n")
    (root / "tb" / "tb_fifo.sv").write_text("module tb_fifo;\n  fifo dut();\n  // FULL check\nendmodule\n")
    (root / "node_modules" / "skip.sv").write_text("module fifo_in_deps;\n")
    os.utime(root / "rtl" / "sync.sv", (time.time() + 10, time.time() + 10))
    return Workspace(root)


async def test_the_tools_offered_are_claude_codes():
    names = [tool["name"] for tool in agent.tools_for(sandboxed=True)]
    assert names == ["Bash", "Read", "Write", "Edit", "MultiEdit", "Glob", "Grep", "LS"]
    assert [tool["name"] for tool in agent.tools_for(sandboxed=True, read_only=True)] == ["Read", "Glob", "Grep", "LS"]
    assert "Bash" not in [tool["name"] for tool in agent.tools_for(sandboxed=False)]
    edit = next(tool for tool in agent.TOOLS if tool["name"] == "Edit")["parameters"]
    assert edit["required"] == ["file_path", "old_string", "new_string"] and "replace_all" in edit["properties"]


async def test_glob_finds_files_newest_first_without_build_folders(ws):
    found = (await ws.call("Glob", {"pattern": "**/*.sv"})).splitlines()
    assert found[0] == "rtl/sync.sv" and sorted(found) == ["rtl/fifo.sv", "rtl/sync.sv", "tb/tb_fifo.sv"]
    assert "node_modules" not in await ws.call("Glob", {"pattern": "**/*.sv"})
    assert await ws.call("Glob", {"pattern": "*.sv", "path": "tb"}) == "tb/tb_fifo.sv"
    assert await ws.call("Glob", {"pattern": "**/*.{sv,v}"}) != "No files found"
    assert await ws.call("Glob", {"pattern": "**/*.vhd"}) == "No files found"


async def test_grep_lists_files_shows_lines_with_context_or_counts(ws):
    assert sorted((await ws.call("Grep", {"pattern": r"module \w*fifo"})).splitlines()) == ["rtl/fifo.sv", "tb/tb_fifo.sv"]
    content = await ws.call("Grep", {"pattern": "full", "-i": True, "output_mode": "content", "glob": "*.sv"})
    assert content.splitlines() == ["rtl/fifo.sv:2:  logic full;", "tb/tb_fifo.sv:3:  // FULL check"]
    around = await ws.call("Grep", {"pattern": "full", "output_mode": "content", "-A": 1, "path": "rtl"})
    assert around.splitlines() == ["rtl/fifo.sv:2:  logic full;", "rtl/fifo.sv-3-  logic empty;"]
    assert await ws.call("Grep", {"pattern": "module", "output_mode": "count", "path": "rtl/fifo.sv"}) == "rtl/fifo.sv:2"
    assert await ws.call("Grep", {"pattern": "nothing-like-this"}) == "No matches found"
    assert (await ws.call("Grep", {"pattern": "(unclosed"})).startswith("Error: invalid regular expression")
    capped = await ws.call("Grep", {"pattern": "e", "output_mode": "content", "head_limit": 2})
    assert len(capped.splitlines()) == 3 and "output cut at 2 lines" in capped


async def test_read_numbers_lines_and_reads_from_the_end(ws):
    assert await ws.call("Read", {"file_path": "rtl/fifo.sv", "offset": 2, "limit": 2}) == (
        "     2\t  logic full;\n     3\t  logic empty;\n[lines 2-3 of 4; pass offset to read more]")
    assert await ws.call("Read", {"file_path": "rtl/fifo.sv", "offset": -1}) == "     4\tendmodule\n[lines 4-4 of 4; pass offset to read more]"
    assert "is a directory: use LS" in await ws.call("Read", {"file_path": "rtl"})
    assert "past the end" in await ws.call("Read", {"file_path": "rtl/sync.sv", "offset": 9})


async def test_edit_needs_a_unique_match_unless_replace_all(ws):
    assert "occurs 2 times" in await ws.call("Edit", {"file_path": "rtl/fifo.sv", "old_string": "logic", "new_string": "bit"})
    result = await ws.call("Edit", {"file_path": "rtl/fifo.sv", "old_string": "logic", "new_string": "bit", "replace_all": True})
    assert result.startswith("Edited rtl/fifo.sv (2 replacements):") and "     2\t  bit full;" in result
    assert "are the same" in await ws.call("Edit", {"file_path": "rtl/fifo.sv", "old_string": "bit", "new_string": "bit"})
    assert "use Write" in await ws.call("Edit", {"file_path": "rtl/new.sv", "old_string": "a", "new_string": "b"})


async def test_multi_edit_applies_all_edits_or_none(ws):
    before = (ws.root / "rtl/fifo.sv").read_text()
    failed = await ws.call("MultiEdit", {"file_path": "rtl/fifo.sv", "edits": [
        {"old_string": "full", "new_string": "is_full"}, {"old_string": "missing", "new_string": "x"}]})
    assert failed.startswith("Error: edit 2: old_string was not found")
    assert (ws.root / "rtl/fifo.sv").read_text() == before
    done = await ws.call("MultiEdit", {"file_path": "rtl/fifo.sv", "edits": [
        {"old_string": "full", "new_string": "is_full"}, {"old_string": "empty", "new_string": "is_empty"}]})
    assert done.startswith("Edited rtl/fifo.sv (2 edits):")
    assert "is_full" in (ws.root / "rtl/fifo.sv").read_text() and "is_empty" in (ws.root / "rtl/fifo.sv").read_text()


async def test_a_turns_reads_run_at_the_same_time_and_writes_in_order(ws, monkeypatch):
    running, peak = 0, 0
    original = ws.read

    def slow_read(args):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        time.sleep(0.05)
        running -= 1
        return original(args)

    monkeypatch.setattr(ws, "read", slow_read)
    results = await ws.run_all([("Read", {"file_path": "rtl/fifo.sv"}), ("Read", {"file_path": "rtl/sync.sv"}),
                                ("Grep", {"pattern": "module"})])
    assert peak == 2 and all(ok for _, ok in results)
    assert [ok for _, ok in await ws.run_all([("Write", {"file_path": "a.txt", "content": "1"}),
                                             ("Read", {"file_path": "a.txt"})])] == [True, True]


def test_the_log_describes_each_call_briefly():
    assert agent.describe_call("Bash", {"command": "make sim"}) == "make sim"
    assert agent.describe_call("Grep", {"pattern": "full", "glob": "*.sv"}) == "full (*.sv)"
    assert agent.describe_call("MultiEdit", {"file_path": "a.sv", "edits": [{}, {}]}) == "a.sv (2 edits)"
    assert agent.describe_call("Read", {"file_path": "a.sv", "offset": 10}) == "a.sv (from line 10)"


def test_long_command_output_keeps_its_start_and_end():
    text = "start\n" + "x" * 100_000 + "\nend"
    clipped = agent._clip(text)
    assert clipped.startswith("start") and clipped.endswith("end") and "characters cut" in clipped


async def test_a_review_searches_only_what_it_may_read(tmp_path):
    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("SECRET_KEY = 1\n")
    (root / ".env").write_text("SECRET_KEY = 2\n")
    review = Workspace(root, read_only=True)
    found = await review.call("Grep", {"pattern": "SECRET_KEY"})
    assert found == "src/app.py"
    assert (await review.call("Bash", {"command": "ls"})).startswith("Error: review workspace is read-only")
    assert asyncio.iscoroutinefunction(review.run_all)


async def test_a_steps_reasoning_effort_follows_its_difficulty(database):
    """Thinking as hard as possible on every turn of an easy step only makes it slower."""
    from super_partner.executor import Executor
    from super_partner.providers import ProviderResult
    from test_leading import MODELS

    asked = []

    class Provider:
        def configured_models(self, ids):
            return set(ids)

        async def complete(self, model_id, prompt, workspace=None, **kwargs):
            asked.append(kwargs.get("effort"))
            return ProviderResult("做完了", 0, 0, model_id)

    run = database.get_run("r")
    run.plan.nodes[0].difficulty = "easy"
    database.save_run(run)
    await Executor(database, Provider(), MODELS).execute("r")
    assert asked[0] == "low"


def test_a_long_tool_loop_history_is_shortened_once_past_the_limit():
    import json

    from super_partner import providers

    big = "x" * 30_000
    write = {"id": "w", "type": "function", "function": {"name": "Write", "arguments": json.dumps(
        {"file_path": "a.sv", "content": big})}}
    history = [{"role": "user", "content": "task"}, {"role": "assistant", "content": None, "tool_calls": [write]}]
    history += [{"role": "tool", "tool_call_id": f"c{i}", "content": f"result {i} " + big} for i in range(10)]
    assert not providers.compact(history[:4])  # short histories stay as they are
    assert providers.compact(history)
    contents = [message["content"] for message in history if message.get("role") == "tool"]
    assert all(len(text) < 600 and "left out" in text for text in contents[:4])
    assert all(len(text) > 30_000 for text in contents[4:])  # the last ones stay whole
    args = json.loads(history[1]["tool_calls"][0]["function"]["arguments"])
    assert args["file_path"] == "a.sv" and "left out" in args["content"] and len(args["content"]) < 600
    before = json.dumps(history)
    providers.compact(history)
    assert json.dumps(history) == before or len(json.dumps(history)) <= len(before)


def test_an_api_step_is_estimated_turn_by_turn_with_its_cache():
    from super_partner import routing
    from super_partner.budget import token_cost
    from super_partner.models import ModelProfile, TaskNode

    api = ModelProfile(id="deepseek:flash", provider="deepseek", display_name="flash", context_window=1_000_000,
                       input_per_million=0.15, output_per_million=0.6, cached_input_per_million=0.003)
    cli = ModelProfile(id="cli:claude", provider="anthropic", display_name="cli", channel="cli",
                       context_window=1_000_000, input_per_million=0, output_per_million=0)
    step = TaskNode(id="a", title="A", instructions="i", capability="coding", difficulty="normal",
                    estimated_input_tokens=4000, estimated_output_tokens=800)
    one_call = token_cost(api, 4000, 800)
    looped = routing.node_cost(step, api)
    # Fifteen turns of a growing, mostly cached conversation: far more than one call.
    assert looped > 5 * one_call
    context, expected = routing.LOOP_BASE_TOKENS + 4000, 0.0
    for turn in range(15):
        expected += token_cost(api, context, 600, context - routing.LOOP_TURN_GROWTH if turn else 0)
        context += routing.LOOP_TURN_GROWTH
    expected += token_cost(api, context, 800, context - routing.LOOP_TURN_GROWTH)
    assert looped == expected
    assert routing.node_cost(step, cli) == 0
    # A hard step takes more turns and thinks harder.
    assert routing.node_cost(step.model_copy(update={"difficulty": "hard"}), api) > looped


async def test_how_to_use_the_tools_is_a_system_prompt_for_every_provider(tmp_path, monkeypatch):
    from test_providers import registry_with

    for name in ("DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.setenv(name, "test")
    sent = []
    registry = registry_with([
        {"choices": [{"message": {"content": "好了"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}},
        {"content": [{"type": "text", "text": "done"}], "stop_reason": "end_turn", "usage": {"input_tokens": 1, "output_tokens": 1}},
        {"candidates": [{"content": {"role": "model", "parts": [{"text": "done"}]}}]},
    ], sent)
    try:
        for model in ("deepseek:deepseek-flash", "anthropic:claude-x", "google:gemini-x"):
            await registry.complete(model, "写一个待办工具", tmp_path, act=True)
    finally:
        await registry.client.aclose()
    deepseek, anthropic, gemini = sent
    assert deepseek["messages"][0]["role"] == "system" and "Bash, Read, Write" in deepseek["messages"][0]["content"]
    assert deepseek["messages"][1] == {"role": "user", "content": "写一个待办工具"}
    assert "Bash, Read, Write" in anthropic["system"] and anthropic["messages"][0]["content"] == "写一个待办工具"
    assert "Bash, Read, Write" in gemini["systemInstruction"]["parts"][0]["text"]
    assert "batch them" in deepseek["messages"][0]["content"]
