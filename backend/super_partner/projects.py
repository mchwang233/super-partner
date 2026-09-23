from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
import subprocess
from pathlib import Path

from .config import settings

MAX_CONTEXT_ENTRIES = 60
MAX_BROWSE_ENTRIES = 500


class ProjectDirError(ValueError):
    pass


class NativePickerError(RuntimeError):
    pass


NATIVE_PICKER_TIMEOUT = 300


def native_picker_status() -> dict:
    """The API runs on the user's own machine, so it can open the desktop's folder dialog."""
    if not os.getenv("DISPLAY") and not os.getenv("WAYLAND_DISPLAY"):
        return {"available": False, "reason": "No desktop session is attached to the API process"}
    if not shutil.which("zenity"):
        return {"available": False, "reason": "zenity is not installed"}
    return {"available": True, "reason": None}


async def native_pick_directory() -> Path | None:
    """Open the desktop folder dialog; returns None when the user cancels it."""
    status = native_picker_status()
    if not status["available"]:
        raise NativePickerError(status["reason"])
    process = await asyncio.create_subprocess_exec(
        "zenity", "--file-selection", "--directory", "--title=Choose a project folder",
        f"--filename={allowed_roots()[0]}/",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=NATIVE_PICKER_TIMEOUT)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        await process.wait()
        raise
    if process.returncode == 1:
        return None
    if process.returncode:
        raise NativePickerError(stderr.decode(errors="replace").strip()[:200] or "Folder dialog failed")
    return resolve_project_dir(stdout.decode().strip())


def allowed_roots() -> list[Path]:
    return [root.expanduser().resolve() for root in settings.project_roots]


def _is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _existing_dir_in_roots(raw: str) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ProjectDirError("Project directory must be an absolute path")
    path = path.resolve()
    if not path.is_dir():
        raise ProjectDirError(f"Project directory does not exist: {path}")
    roots = allowed_roots()
    if not any(_is_within(path, root) for root in roots):
        allowed = ", ".join(str(root) for root in roots)
        raise ProjectDirError(f"Project directory must be inside an allowed root: {allowed}")
    return path


def resolve_project_dir(raw: str) -> Path:
    """Validate a user-supplied project directory and return its canonical path."""
    path = _existing_dir_in_roots(raw)
    if _is_within(path, settings.data_dir.resolve()):
        raise ProjectDirError("Project directory cannot be inside the Super Partner data directory")
    return path


def browse_directories(raw: str | None = None, show_hidden: bool = False) -> dict:
    """List subdirectories for the web console's folder picker, confined to the allowed roots."""
    roots = allowed_roots()
    path = _existing_dir_in_roots(raw) if raw else roots[0]
    data_dir = settings.data_dir.resolve()
    entries = []
    try:
        children = sorted(path.iterdir(), key=lambda entry: entry.name.lower())
    except PermissionError as exc:
        raise ProjectDirError(f"Permission denied: {path}") from exc
    for child in children:
        if (child.name.startswith(".") and not show_hidden) or not child.is_dir():
            continue
        resolved = child.resolve()
        # Skip symlinks that leave the allowed roots and the Super Partner data directory.
        if not any(_is_within(resolved, root) for root in roots) or _is_within(resolved, data_dir):
            continue
        entries.append({"name": child.name, "path": str(resolved), "git": (child / ".git").exists()})
        if len(entries) >= MAX_BROWSE_ENTRIES:
            break
    parent = path.parent if path not in roots and any(_is_within(path.parent, r) for r in roots) else None
    return {
        "path": str(path), "parent": str(parent) if parent else None,
        "git": (path / ".git").exists(), "roots": [str(root) for root in roots],
        "entries": entries, "truncated": len(entries) >= MAX_BROWSE_ENTRIES,
    }


def _git(path: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(path), *args], capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def git_snapshot(path: Path) -> dict:
    """Record where the project started so the user can diff or revert agent edits."""
    head = _git(path, "rev-parse", "HEAD")
    if head is None:
        return {"git": False}
    status = _git(path, "status", "--porcelain") or ""
    return {
        "git": True, "head": head, "branch": _git(path, "rev-parse", "--abbrev-ref", "HEAD"),
        "uncommitted_files": len(status.splitlines()),
    }


def project_context(path: Path) -> str:
    """A short, planner-facing description of the target project."""
    entries = sorted(
        (entry for entry in path.iterdir() if entry.name != ".git"),
        key=lambda entry: (not entry.is_dir(), entry.name.lower()),
    )
    names = [f"{entry.name}/" if entry.is_dir() else entry.name for entry in entries[:MAX_CONTEXT_ENTRIES]]
    if len(entries) > MAX_CONTEXT_ENTRIES:
        names.append(f"... ({len(entries) - MAX_CONTEXT_ENTRIES} more)")
    snapshot = git_snapshot(path)
    git_line = (
        f"git branch {snapshot['branch']}, {snapshot['uncommitted_files']} uncommitted file(s)"
        if snapshot["git"] else "not a git repository"
    )
    return f"Target project: {path} ({git_line})\nTop-level entries: {', '.join(names) or '(empty)'}"
