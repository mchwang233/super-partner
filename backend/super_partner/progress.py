"""Checking on a long attempt instead of cutting it off: after a while, another model looks at
what the attempt has been doing and says whether it is still getting somewhere or is stuck.
Only a stuck attempt is stopped; it then fails like any other attempt."""

from __future__ import annotations

import asyncio
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
# A work package past twice its expected turns goes to the task's lead (supervisor.overrun_message),
# and again at every further multiple; how the error reads when the lead stops it.
OVERRUN_FACTOR = 2
OVERRUN = "超出预计轮数，负责人已停止这次尝试："
EXPECTED_TURNS = {"easy": 10, "normal": 15, "hard": 25}


def expected_turns(node: TaskNode) -> Optional[int]:
    """Tool turns a scoped work package should take (planner.package); None for other steps."""
    if not (node.files or node.reads) or node.verifies:
        return None
    return EXPECTED_TURNS.get(node.difficulty, EXPECTED_TURNS["normal"])
VERDICT = re.compile(r"^\s*VERDICT:\s*(CONTINUE|STOP)\b:?\s*(.*)$", re.IGNORECASE | re.MULTILINE)
# Tool calls of an API model shown to the checker, latest last.
RECENT_CALLS = 40
# Files a CLI changed, and processes it runs, shown to the checker.
MAX_LISTED = 40
SKIP_DIRS = {".git", ".review", "node_modules", "__pycache__", ".venv", "venv"}


def check_prompt(node: TaskNode, minutes: float, evidence: str) -> str:
    return (
        f"A model has been working on one step of a task for {minutes:.0f} minutes. Decide whether "
        "it is still making real progress, or is stuck and should be stopped.\n\n"
        f"Step: {node.title}\n{node.instructions}\n\n"
        + (f"Acceptance criteria:\n{node.acceptance_criteria}\n\n" if node.acceptance_criteria else "")
        + f"What it has done so far:\n{evidence}\n\n"
        "Long work that keeps moving (new files, fixes, checks that change) is fine: let it continue. "
        "It is stuck when it repeats the same calls or commands, fails the same way again and again "
        "without changing approach, waits on something that will not come (a server that never "
        "starts, a prompt nobody answers), or has drifted away from the step. When in doubt, continue.\n"
        "End with exactly one line: `VERDICT: CONTINUE` or `VERDICT: STOP: <why, in Chinese>`."
    )


def parse(text: str) -> Optional[str]:
    """Why to stop, or None to continue (also when the answer has no verdict)."""
    matches = VERDICT.findall(text or "")
    if not matches or matches[-1][0].upper() != "STOP":
        return None
    return matches[-1][1].strip() or "进度检查判断这次尝试已经卡住。"


def api_evidence(turns: int, calls: list[dict]) -> str:
    """An API model's turns and its latest tool calls."""
    failed = sum(1 for call in calls if not call.get("ok", True))
    lines = [f"{turns} turns, {len(calls)} tool calls so far, {failed} of them failed. Latest calls:"]
    lines += [f"- turn {call['turn']}: {call['tool']} {call['detail']}{'' if call.get('ok', True) else '  (failed)'}"
              for call in calls[-RECENT_CALLS:]]
    return "\n".join(lines)


def changed_since(root: Path, since: float) -> list[str]:
    """Files under ``root`` modified after ``since`` (epoch seconds), newest first."""
    found = []
    try:
        for path in root.rglob("*"):
            if any(part in SKIP_DIRS for part in path.relative_to(root).parts):
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            if path.is_file() and stat.st_mtime > since:
                found.append((stat.st_mtime, path.relative_to(root).as_posix()))
    except OSError:
        pass
    return [name for _mtime, name in sorted(found, reverse=True)]


async def processes(group: int) -> list[str]:
    """Commands running in a process group, with how long each has run."""
    try:
        process = await asyncio.create_subprocess_exec(
            "ps", "-o", "etime=,args=", "-g", str(group),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        output, _ = await asyncio.wait_for(process.communicate(), timeout=10)
    except (OSError, asyncio.TimeoutError):
        return []
    return [line.strip()[:200] for line in output.decode(errors="replace").splitlines() if line.strip()]


async def cli_evidence(root: Path, started: float, last_check: float, group: int) -> str:
    """What a CLI agent, whose own steps are not visible, has changed and is running."""
    recent = await asyncio.to_thread(changed_since, root, last_check)
    overall = await asyncio.to_thread(changed_since, root, started)
    running = await processes(group)
    since = (time.time() - last_check) / 60
    return "\n".join([
        f"Files changed since the start: {len(overall)}; in the last {since:.0f} minutes: {len(recent)}.",
        *(f"- {name}" for name in recent[:MAX_LISTED]),
        "Processes it is running (elapsed time, command):",
        *(f"- {line}" for line in running[:MAX_LISTED]),
    ])
