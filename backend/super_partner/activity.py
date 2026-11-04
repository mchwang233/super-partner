"""What a step is doing while it runs, for its page. An API model's tool calls already go into the
executor's tool log as it makes them; a CLI's commands, file changes and messages are added to the
same log as it prints them (only Codex prints them as it goes). The log becomes the attempt's
tool calls when it ends, so a CLI attempt keeps them too."""

from __future__ import annotations

import contextlib
import json
from contextvars import ContextVar
from typing import Optional

# The tool log of the step whose model runs in this context; None outside a step's own work.
current: ContextVar[Optional[list]] = ContextVar("step_activity", default=None)
MAX_OUTPUT = 2000
MAX_DETAIL = 400


@contextlib.contextmanager
def recording(log: list):
    token = current.set(log)
    try:
        yield
    finally:
        current.reset(token)


@contextlib.contextmanager
def paused():
    """Calls made on the step's behalf (a progress check) are not the step's own work."""
    token = current.set(None)
    try:
        yield
    finally:
        current.reset(token)


def codex_entry(event: dict) -> Optional[dict]:
    """A tool log entry for one Codex JSON event; None for events that show no work."""
    item = event.get("item") if isinstance(event.get("item"), dict) else None
    if not item or event.get("type") not in {"item.started", "item.updated", "item.completed"}:
        return None
    kind, done = item.get("type"), event.get("type") == "item.completed"
    entry = {"turn": 0, "id": item.get("id")}
    if kind == "command_execution":
        code = item.get("exit_code")
        status = item.get("status")
        ok = (False if status == "failed" or (code is not None and code != 0) else
              True if done and code == 0 else None)
        state = ("failed" if ok is False else "succeeded" if ok is True else
                 "incomplete" if status in {"cancelled", "interrupted"} else "unknown" if done else "running")
        entry.update(tool="command", detail=str(item.get("command") or "")[:MAX_DETAIL],
                     ok=ok, running=state == "running", status=state, exit_code=code,
                     output=str(item.get("aggregated_output") or "")[-MAX_OUTPUT:])
    elif kind == "file_change" and done:
        changes = item.get("changes") or []
        entry.update(tool="file_change", ok={"completed": True, "failed": False}.get(item.get("status")),
                     detail="、".join(f"{c.get('kind', '')} {c.get('path', '')}".strip()
                                     for c in changes if isinstance(c, dict))[:MAX_DETAIL])
    elif kind == "agent_message" and done:
        entry.update(tool="message", detail=str(item.get("text") or "")[:MAX_DETAIL], ok=True)
    elif kind == "todo_list":
        todos = [t for t in item.get("items") or [] if isinstance(t, dict)]
        entry.update(tool="plan", ok=True, detail="；".join(
            ("✓ " if t.get("completed") else "") + str(t.get("text") or "") for t in todos)[:MAX_DETAIL])
    else:
        return None
    return entry


def note_line(line: bytes) -> None:
    """One line a CLI printed, for the step running in this context (if any)."""
    log = current.get()
    if log is None or not line.strip():
        return
    try:
        event = json.loads(line)
    except (ValueError, UnicodeDecodeError):
        return
    entry = codex_entry(event) if isinstance(event, dict) else None
    if not entry:
        return
    if entry.get("id"):
        for index in range(len(log) - 1, -1, -1):
            if log[index].get("id") == entry["id"]:
                log[index] = entry  # a command that started now has its result
                return
    # Presentation limits must not discard starts or prevent later completion events merging.
    log.append(entry)
