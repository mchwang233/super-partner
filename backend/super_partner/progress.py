"""Checking on a long attempt instead of cutting it off: after a while, another model looks at
what the attempt has been doing and says whether it is still getting somewhere or is stuck.
Only a stuck attempt is stopped; it then fails like any other attempt."""

from __future__ import annotations

import asyncio
import os
import re
import time
from collections.abc import Awaitable
from pathlib import Path
from typing import Callable, Optional

from .models import TaskNode

# Looks at the evidence of an attempt so far; returns why to stop it, or None to let it go on.
Watch = Callable[[str], Awaitable[Optional[str]]]

class Stopped(RuntimeError):
    """A CLI stopped because a progress check found it stuck."""


# How the error of a stopped attempt reads.
STOPPED = "进度检查判断这次尝试卡住，已停止："
# Told to the checker when the model has not answered its latest request yet.
WAITING = "It is now waiting for its model's reply to one request: {minutes:.0f} minutes so far, nothing back yet."
# A work package past twice its expected turns goes to the task's lead (supervisor.overrun_message),
# and again at every further multiple; how the error reads when the lead stops it.
OVERRUN_FACTOR = 2
OVERRUN = "超出预计轮数，负责人已停止这次尝试："
CRITICAL_OVERRUN = "这一步挡着后面的步骤，用时远超预期，负责人已停止这次尝试（可以换更快的模型）："
EXPECTED_TURNS = {"easy": 10, "normal": 15, "hard": 25}


def limit_reason(node: TaskNode) -> Optional[str]:
    """Hard declared limits survive retries, rework and model switches."""
    contract = node.artifact_contract
    if not contract:
        return None
    if node.contract_turns >= contract.max_turns:
        return f"达到声明轮次上限 {contract.max_turns}，停止工作包；保留产物与日志供诊断。"
    if node.contract_cost_usd >= contract.max_cost_usd:
        return f"达到声明费用上限 ${contract.max_cost_usd:g}，停止工作包；保留产物与日志供诊断。"
    return None


def expected_turns(node: TaskNode) -> Optional[int]:
    """Tool turns a scoped work package should take (planner.package); None for other steps."""
    if not (node.files or node.reads) or node.verifies:
        return None
    return EXPECTED_TURNS.get(node.difficulty, EXPECTED_TURNS["normal"])
VERDICT = re.compile(r"^\s*VERDICT:\s*(CONTINUE|STOP)\b:?\s*(.*)$", re.IGNORECASE | re.MULTILINE)
# Tool calls of an API model shown to the checker, latest last.
RECENT_CALLS = 40
# Tool entries of a CLI shown to the checker, latest last, and how much of each output.
RECENT_ENTRIES = 6
ENTRY_OUTPUT = 500
# Files a CLI changed, and processes it runs, shown to the checker.
MAX_LISTED = 40
# How much of a command line is shown; long paths must not hide what it actually runs.
COMMAND_CHARS = 200
SKIP_DIRS = {".git", ".review", "node_modules", "__pycache__", ".venv", "venv"}


def check_prompt(node: TaskNode, minutes: float, evidence: str) -> str:
    return (
        f"A model has been working on one step of a task for {minutes:.0f} minutes. Decide whether "
        "it is still making real progress, or is stuck and should be stopped.\n\n"
        f"Step: {node.title}\n{node.instructions}\n\n"
        + (f"Acceptance criteria:\n{node.acceptance_criteria}\n\n" if node.acceptance_criteria else "")
        + f"What it has done so far:\n{evidence}\n\n"
        "Long work that keeps moving (new files, fixes, checks that change) is fine: let it continue. "
        "A compile, simulation or regression run that keeps writing logs or files, or whose child "
        "processes are still running, is making progress even when one command has taken a long "
        "time: let it continue. "
        "It is stuck when it repeats the same calls or commands, fails the same way again and again "
        "without changing approach, waits on something that will not come (a server that never "
        "starts, a prompt nobody answers, a model reply that has not come for more than 8 minutes), "
        "or has drifted away from the step. When in doubt, continue.\n"
        "End with exactly one line: `VERDICT: CONTINUE` or `VERDICT: STOP: <why, in Chinese>`."
    )


def parse(text: str) -> Optional[str]:
    """Only an explicit, unambiguous CONTINUE permits another interval of paid work."""
    matches = VERDICT.findall(text or "")
    if len(matches) != 1:
        return "进度检查未给出唯一有效结论，停止本次尝试并交由负责人判断。"
    if matches[-1][0].upper() != "STOP":
        return None
    return matches[-1][1].strip() or "进度检查判断这次尝试已经卡住。"


def api_evidence(turns: int, calls: list[dict]) -> str:
    """An API model's turns and its latest tool calls."""
    failed = sum(1 for call in calls if not call.get("ok", True))
    lines = [f"{turns} turns, {len(calls)} tool calls so far, {failed} of them failed. Latest calls:"]
    lines += [f"- turn {call['turn']}: {call['tool']} {call['detail']}{'' if call.get('ok', True) else '  (failed)'}"
              + (f"\n  Evidence: {call['output'][-500:]}" if call.get("output") else "")
              for call in calls[-RECENT_CALLS:]]
    return "\n".join(lines)


def file_state(root: Path) -> dict[str, tuple[float, int]]:
    """Modification time and size of the files under ``root``, skipping build and VCS folders."""
    state: dict[str, tuple[float, int]] = {}
    try:
        for folder, dirs, files in os.walk(root):
            dirs[:] = [name for name in dirs if name not in SKIP_DIRS]
            for name in files:
                path = Path(folder, name)
                try:
                    stat = path.stat()
                except OSError:
                    continue
                state[path.relative_to(root).as_posix()] = (stat.st_mtime, stat.st_size)
    except OSError:
        pass
    return state


def changed_since(root: Path, since: float) -> list[str]:
    """Files under ``root`` modified after ``since`` (epoch seconds), newest first."""
    found = [(mtime, name) for name, (mtime, _size) in file_state(root).items() if mtime > since]
    return [name for _mtime, name in sorted(found, reverse=True)]


def _size(value: int) -> str:
    if value < 1024:
        return f"{value} B"
    for unit in ("KB", "MB", "GB"):
        value /= 1024
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}"
    return f"{value:.1f} GB"


def _ago(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f} seconds"
    if seconds < 5400:
        return f"{seconds / 60:.0f} minutes"
    return f"{seconds / 3600:.1f} hours"


def _clip(text: str, limit: int = COMMAND_CHARS) -> str:
    """At most ``limit`` characters of a long line: its start and its end, where a regression
    command usually names the simulator, its test and its log."""
    if len(text) <= limit:
        return text
    head = limit // 2
    return f"{text[:head]}…{text[-(limit - head - 1):]}"


def _process_rows(output: bytes) -> dict[int, tuple[int, int, int, str, str]]:
    rows = {}
    for line in output.decode(errors="replace").splitlines():
        parts = line.split(None, 5)
        if len(parts) < 6 or not all(part.isdigit() for part in parts[:4]):
            continue
        pid, ppid, pgid, sid = (int(part) for part in parts[:4])
        rows[pid] = (ppid, pgid, sid, parts[4], parts[5].strip())
    return rows


async def _group_processes(group: int) -> list[str]:
    try:
        process = await asyncio.create_subprocess_exec(
            "ps", "-ww", "-o", "etime=,args=", "-g", str(group),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        output, _ = await asyncio.wait_for(process.communicate(), timeout=10)
    except (OSError, asyncio.TimeoutError):
        return []
    return [_clip(line.strip()) for line in output.decode(errors="replace").splitlines() if line.strip()]


async def processes(group: int) -> list[str]:
    """The CLI and every process it started, with how long each has run. A tool may put its
    commands in a session or process group of their own, so the whole tree below the CLI counts,
    not only the CLI's own group."""
    try:
        process = await asyncio.create_subprocess_exec(
            # -ww: without it ps cuts commands at the terminal width, hiding what a long
            # regression's child actually runs.
            "ps", "-ww", "-eo", "pid=,ppid=,pgid=,sid=,etime=,args=",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        output, _ = await asyncio.wait_for(process.communicate(), timeout=10)
    except (OSError, asyncio.TimeoutError):
        return await _group_processes(group)
    rows = _process_rows(output)
    if process.returncode or not rows:
        return await _group_processes(group)
    keep = {pid for pid, (_ppid, pgid, sid, _etime, _args) in rows.items()
            if pid == group or pgid == group or sid == group}
    growing = True
    while growing:
        growing = False
        for pid, (ppid, _pgid, _sid, _etime, _args) in rows.items():
            if pid not in keep and ppid in keep:
                keep.add(pid)
                growing = True
    return [f"{rows[pid][3]} {_clip(rows[pid][4])}" for pid in sorted(keep)]


def _log_lines(log: Optional[list]) -> list[str]:
    if not log:
        return []
    lines = ["Its latest tool calls and output:"]
    for entry in log[-RECENT_ENTRIES:]:
        if not isinstance(entry, dict):
            continue
        state = entry.get("status") or ("running" if entry.get("running") else
                                        "succeeded" if entry.get("ok") is True else
                                        "failed" if entry.get("ok") is False else "unknown")
        flag = "" if state == "succeeded" else f" ({state})"
        lines.append(f"- {entry.get('tool') or 'tool'}: {str(entry.get('detail') or '')[:200]}{flag}")
        output = str(entry.get("output") or "").strip()
        if output:
            lines.append("  Output: " + output[-ENTRY_OUTPUT:].replace("\n", "\n  "))
    return lines


class CliProgress:
    """Evidence about one CLI attempt; remembers file sizes to show what grew between checks."""

    def __init__(self, root: Path, started: float, group: int) -> None:
        self.root, self.started, self.group = Path(root), started, group
        self.previous: dict[str, tuple[float, int]] = {}
        self.output_total = 0

    async def evidence(self, last_check: float, log: Optional[list] = None, printed: int = 0) -> str:
        now = time.time()
        state = await asyncio.to_thread(file_state, self.root)
        if self.previous:
            # Anything not exactly as at the last check changed since then, even a log written
            # just before that check ended.
            changed = [(mtime, name, size) for name, (mtime, size) in state.items()
                       if self.previous.get(name) != (mtime, size)]
        else:
            changed = [(mtime, name, size) for name, (mtime, size) in state.items() if mtime > last_check]
        recent = sorted(changed, reverse=True)
        overall = [(name, size) for name, (mtime, size) in state.items() if mtime > self.started]
        growth = sum(max(0, size - self.previous[name][1]) for _mtime, name, size in changed
                     if name in self.previous)
        self.output_total += max(0, printed)
        minutes = max(0.0, (now - last_check) / 60)
        lines = [f"Files changed since the start: {len(overall)}; in the last {minutes:.0f} minutes: "
                 f"{len(recent)}" + (f", {_size(growth)} more than at the last check" if growth else "") + "."]
        for mtime, name, size in recent[:MAX_LISTED]:
            old = self.previous.get(name)
            change = ("new" if old is None else
                      (f"{_size(size - old[1])} larger" if size > old[1] else "changed"))
            lines.append(f"- {name}: {_size(size)}, {change}, modified {_ago(now - mtime)} ago")
        if printed > 0:
            lines.append(f"It printed {_size(printed)} of output since the last check "
                         f"({_size(self.output_total)} in all).")
        running = await processes(self.group)
        lines.append("Processes it is running, children included (elapsed time, command):")
        lines.extend(f"- {line}" for line in running[:MAX_LISTED])
        lines.extend(_log_lines(log))
        self.previous = state
        return "\n".join(lines)


async def cli_evidence(root: Path, started: float, last_check: float, group: int,
                       log: Optional[list] = None, printed: int = 0) -> str:
    """What a CLI agent, whose own steps are not visible, has changed and is running."""
    return await CliProgress(root, started, group).evidence(last_check, log=log, printed=printed)
