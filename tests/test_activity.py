"""What a running step is doing, live (activity.py), and the limits of contract steps
(contracts.MIN_TOOL_CALLS, the deliverables-first prompt)."""
import asyncio
import json
import sys

from super_partner import activity, contracts
from super_partner.cli_channels import CliChannelRegistry
from super_partner.config import settings
from super_partner.models import ExecutionRun, RunStatus, TaskNode, TaskPlan


def codex(kind, **item):
    return json.dumps({"type": kind, "item": item}).encode()


def test_codex_events_become_tool_log_entries_and_a_finished_command_replaces_its_start():
    log = []
    with activity.recording(log):
        activity.note_line(b'{"type":"turn.started"}')
        activity.note_line(b"not json")
        activity.note_line(codex("item.completed", id="m", type="agent_message", text="先写 RTL"))
        activity.note_line(codex("item.started", id="c", type="command_execution", command="vcs -full64",
                                 aggregated_output="", exit_code=None, status="in_progress"))
        assert log[-1]["running"] and log[-1]["ok"] is None
        activity.note_line(codex("item.completed", id="c", type="command_execution", command="vcs -full64",
                                 aggregated_output="Error-[SE]", exit_code=1, status="completed"))
        activity.note_line(codex("item.completed", id="f", type="file_change",
                                 changes=[{"path": "rtl/fifo.sv", "kind": "add"}], status="completed"))
    assert [(e["tool"], e["detail"]) for e in log] == [
        ("message", "先写 RTL"), ("command", "vcs -full64"), ("file_change", "add rtl/fifo.sv")]
    assert log[1]["ok"] is False and not log[1]["running"] and log[1]["output"] == "Error-[SE]"
    assert all(e["turn"] == 0 for e in log)  # a CLI's commands are not counted as turns


def test_nothing_is_recorded_outside_a_step_or_while_paused():
    activity.note_line(codex("item.completed", id="m", type="agent_message", text="x"))
    log = []
    with activity.recording(log), activity.paused():
        activity.note_line(codex("item.completed", id="m", type="agent_message", text="x"))
    assert log == []


async def test_a_cli_step_records_what_it_prints_as_it_goes(tmp_path):
    script = ("import json,sys,time\n"
              "print(json.dumps({'type':'item.completed','item':{'id':'a','type':'agent_message','text':'hi'}}),"
              " flush=True)\n"
              "time.sleep(0.3)\n")
    log, seen = [], []

    async def look():
        await asyncio.sleep(0.2)
        seen.extend(log)

    class Spec:
        executable = "python"

    with activity.recording(log):
        looking = asyncio.ensure_future(look())
        await CliChannelRegistry()._run(Spec(), [sys.executable, "-c", script], tmp_path, None, 30)
        await looking
    assert [e["detail"] for e in seen] == ["hi"]  # there before the command ended


async def test_the_activity_of_a_running_step_is_served(task_api):
    api, client = task_api
    api.db.save_run(ExecutionRun(id="r", prompt="p", status=RunStatus.RUNNING, budget_usd=1, plan=TaskPlan(
        summary="s", nodes=[TaskNode(id="n", title="t", instructions="i", capability="coding")])))
    body = (await client.get("/api/tasks/r/nodes/n/activity")).json()
    assert body == {"running": False, "total": 0, "calls": []}
    api.executor.tool_logs["r", "n"] = [{"turn": 1, "tool": "Bash", "detail": "ls", "ok": True}]
    body = (await client.get("/api/tasks/r/nodes/n/activity")).json()
    assert body["running"] and body["calls"][0]["detail"] == "ls"
    assert (await client.get("/api/tasks/r/nodes/missing/activity")).status_code == 404


def contract(max_turns, difficulty="hard"):
    return {"difficulty": difficulty, "artifact_contract": {
        "inputs": [], "outputs": [{"path": "rtl/fifo.sv"}], "acceptance_commands": ["true"],
        "blocked_when": ["no vcs"], "max_turns": max_turns, "max_cost_usd": 0.3}}


def test_a_tight_limit_is_raised_to_the_floor_for_its_difficulty_with_its_cost(monkeypatch):
    monkeypatch.setattr(settings, "contract_call_floors", True)
    raised = contracts.declarations(contract(30))["artifact_contract"]
    assert raised.max_turns == 80 and raised.max_cost_usd == 0.8
    kept = contracts.declarations(contract(100))["artifact_contract"]
    assert kept.max_turns == 100 and kept.max_cost_usd == 0.3
    assert contracts.declarations(contract(5, "easy"))["artifact_contract"].max_turns == 20


def test_the_contract_prompt_puts_the_deliverables_first():
    node = TaskNode(id="n", title="t", instructions="i", capability="coding", **contracts.declarations(contract(80)))
    node.contract_tool_calls = 12
    text = contracts.prompt(node)
    assert "Write every declared output first (rtl/fifo.sv)" in text and "12 of 80 tool calls" in text
    assert "not yours" in text


def test_completion_matches_the_entire_log_even_beyond_display_limit():
    log = []
    with activity.recording(log):
        for index in range(420):
            activity.note_line(codex("item.started", id=str(index), type="command_execution", command="check"))
        for index in range(420):
            activity.note_line(codex("item.completed", id=str(index), type="command_execution",
                                     command="check", exit_code=0, status="completed"))
    assert len(log) == 420
    assert all(entry["ok"] is True and not entry["running"] for entry in log)


def test_command_completion_without_exit_code_is_unknown_not_failed():
    entry = activity.codex_entry(json.loads(codex("item.completed", id="c", type="command_execution",
                                                command="check", status="completed")))
    assert entry["ok"] is None
    assert entry["status"] == "unknown"
    failed = activity.codex_entry(json.loads(codex("item.completed", id="c", type="command_execution",
                                                 command="check", status="failed", exit_code=0)))
    assert failed["ok"] is False
    assert failed["exit_code"] == 0


def test_historical_unknown_tool_calls_round_trip_without_inventing_success(tmp_path):
    from super_partner.db import Database
    from super_partner.models import NodeAttempt

    database = Database(tmp_path / "history.db")
    body = ExecutionRun(id="r", prompt="p", status=RunStatus.FAILED, budget_usd=1, plan=TaskPlan(summary="s", nodes=[
        TaskNode(id="n", title="t", instructions="i", capability="coding")])).model_dump(mode="json")
    database.save_run(ExecutionRun.model_validate(body))
    body["plan"]["nodes"][0]["attempts"] = [{"outcome": "error", "tool_calls": [
        {"turn": 0, "tool": "command", "ok": None, "id": "pending", "running": True},
        {"turn": 0, "tool": "command"},
        {"turn": 0, "tool": "command", "ok": False, "exit_code": 2},
        {"turn": 0, "tool": "command", "ok": True, "exit_code": 0},
    ]}]
    # Store the legacy JSON directly, so reads must handle null and absent fields themselves.
    with database.connection:
        database.connection.execute("UPDATE runs SET body = ? WHERE id = ?", (json.dumps(body), "r"))
    for run in [database.get_run("r"), *database.list_runs()]:
        attempt = run.plan.nodes[0].attempts[0]
        assert [call.ok for call in attempt.tool_calls] == [None, None, False, True]
        assert attempt.tool_calls[0].id == "pending"
        assert attempt.tool_calls[0].status == "incomplete"
        assert attempt.tool_calls[1].status == "unknown"
        assert attempt.tool_calls[2].exit_code == 2
        assert attempt.result is None
        assert NodeAttempt.model_validate_json(attempt.model_dump_json()) == attempt
        database.save_run(run)


async def test_cli_trace_retains_raw_output_stderr_and_exit_on_failure(tmp_path):
    import base64

    import pytest

    class Spec:
        executable = "python"

    trace = tmp_path / "trace" / "attempt.jsonl"
    output = codex("item.started", id="c", type="command_execution", command="check") + b"\n"
    script = f"import sys; sys.stdout.buffer.write({output!r}); sys.stdout.flush(); sys.stderr.write('problem'); sys.exit(3)"
    with pytest.raises(RuntimeError, match="failed"):
        await CliChannelRegistry()._run(Spec(), [sys.executable, "-c", script], tmp_path, None, 30,
                                        tool_trace=trace)
    records = [json.loads(line) for line in trace.read_text().splitlines()]
    assert b"".join(base64.b64decode(r["data"]) for r in records if r["type"] == "cli.stdout") == output
    assert b"".join(base64.b64decode(r["data"]) for r in records if r["type"] == "cli.stderr") == b"problem"
    assert records[-1]["type"] == "cli.exit" and records[-1]["exit_code"] == 3


def test_unknown_and_incomplete_tools_are_not_reported_as_failures_or_old_answers():
    from super_partner import diagnosis, progress, quality
    from super_partner.models import NodeAttempt

    old = NodeAttempt(outcome="rejected", result="old answer", result_source="provider")
    latest = NodeAttempt(outcome="error", tool_calls=[
        {"turn": 0, "tool": "command", "detail": "unknown check"},
        {"turn": 0, "tool": "command", "detail": "unfinished check", "status": "incomplete"},
    ])
    node = TaskNode(id="n", title="t", instructions="i", capability="coding", result="old answer",
                    attempts=[old, latest])
    run = ExecutionRun(id="r", prompt="p", status=RunStatus.FAILED, budget_usd=1, plan=TaskPlan(summary="s", nodes=[node]))
    history = "\n".join(diagnosis._history(run, []))
    assert "Last result:" not in history
    assert "attempt 1 result" in history
    assert "attempt 2 result: unknown" in history
    summary = diagnosis._tool_summary(latest, True)
    assert "failed" not in summary
    assert "unknown" in summary and "incomplete" in summary
    notes = quality.attempt_notes(node)
    assert "unknown: unknown check" in notes and "incomplete: unfinished check" in notes
    lines = "\n".join(progress._log_lines([c.model_dump() for c in latest.tool_calls]))
    assert "unknown" in lines and "incomplete" in lines and "failed" not in lines


async def test_provider_cli_call_persists_final_answer_and_complete_raw_stream(tmp_path, monkeypatch):
    import base64

    from super_partner import cli_channels
    from super_partner.providers import ProviderRegistry

    trace = tmp_path / "trace.jsonl"
    registry = ProviderRegistry()
    monkeypatch.setattr(cli_channels.shutil, "which", lambda _: sys.executable)
    # Use the real CLI completion and process runner with a portable fake executable.
    original_run = registry.cli._run

    async def run_script(spec, command, cwd, stdin, timeout, watch=None, **kwargs):
        script = "import json; print(json.dumps({'result': '回答', 'usage': {'input_tokens': 1, 'output_tokens': 2}}))"
        return await original_run(spec, [sys.executable, "-c", script], cwd, None, timeout, watch=watch, **kwargs)

    monkeypatch.setattr(registry.cli, "_run", run_script)
    try:
        result = await registry.complete("cli:claude", "work", tmp_path, tool_trace=trace, allow_api_fallback=False)
    finally:
        await registry.client.aclose()
    records = [json.loads(line) for line in trace.read_text().splitlines()]
    assert result.text == "回答"
    raw = b"".join(base64.b64decode(r["data"]) for r in records if r["type"] == "cli.stdout")
    assert json.loads(raw)["result"] == result.text
    assert {"type": "cli.exit", "exit_code": 0} in records
    assert records[-1] == {"type": "cli.result", "text": result.text, "model_id": "cli:claude"}


async def test_cancelled_cli_keeps_partial_trace_and_exit_status(tmp_path):
    import pytest

    class Spec:
        executable = "python"

    trace = tmp_path / "cancelled.jsonl"
    # Wait for observed output instead of assuming a subprocess startup duration.
    script = "import sys,time; print('partial evidence', flush=True); time.sleep(60)"
    task = asyncio.create_task(CliChannelRegistry()._run(
        Spec(), [sys.executable, "-c", script], tmp_path, None, 30, tool_trace=trace))
    try:
        for _ in range(200):
            if trace.exists() and 'cli.stdout' in trace.read_text():
                break
            await asyncio.sleep(.01)
        else:
            pytest.fail("subprocess did not produce its first evidence")
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    records = [json.loads(line) for line in trace.read_text().splitlines()]
    assert any(r["type"] == "cli.stdout" for r in records)
    assert any(r["type"] == "cli.interrupted" for r in records)
    assert records[-1]["type"] == "cli.exit" and records[-1]["exit_code"] != 0
