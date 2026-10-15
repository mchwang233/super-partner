from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
import subprocess
import time
from collections import deque
from pathlib import Path

from .config import settings

MAX_CONTEXT_ENTRIES = 60
MAX_DISCOVERED = 300
DISCOVER_DEPTH = 4
DISCOVER_SECONDS = 3.0
# Dependency, build, and OS folders that hold no projects of their own but are slow to walk.
SKIP_DISCOVERY = {
    "node_modules", "__pycache__", "venv", "site-packages", "vendor", "dist", "build", "target",
    "snap", "Library", "Applications", "Pictures", "Music", "Videos", "Downloads",
}


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


def _picker_start(start: str | None) -> Path:
    """Open the dialog at the requested folder when it is allowed, else at the first root."""
    if start:
        with contextlib.suppress(ProjectDirError):
            return resolve_project_dir(start)
    return allowed_roots()[0]


async def native_pick_directory(start: str | None = None) -> Path | None:
    """Open the desktop folder dialog; returns None when the user cancels it."""
    status = native_picker_status()
    if not status["available"]:
        raise NativePickerError(status["reason"])
    process = await asyncio.create_subprocess_exec(
        "zenity", "--file-selection", "--directory", "--title=选择项目目录",
        f"--filename={_picker_start(start)}/",
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


def _git_marker_time(path: Path) -> float:
    """Recent work touches the git index or HEAD, so their mtime ranks active projects first."""
    times = []
    for name in ("index", "HEAD"):
        with contextlib.suppress(OSError):
            times.append((path / ".git" / name).stat().st_mtime)
    if not times:
        with contextlib.suppress(OSError):
            times.append((path / ".git").stat().st_mtime)
    return max(times, default=0.0)


def discover_projects(
    max_depth: int = DISCOVER_DEPTH, limit: int = MAX_DISCOVERED, seconds: float = DISCOVER_SECONDS,
) -> dict:
    """Find git checkouts under the allowed roots, most recently active first.

    The walk is breadth-first, skips hidden and dependency folders, never follows symlinks out
    of the roots, stops descending at a repository, and gives up after a time budget.
    """
    deadline = time.monotonic() + seconds
    data_dir = settings.data_dir.resolve()
    queue = deque((root, 0) for root in allowed_roots())
    found: list[dict] = []
    truncated = False
    while queue:
        if len(found) >= limit or time.monotonic() > deadline:
            truncated = True
            break
        path, depth = queue.popleft()
        try:
            children = sorted(os.scandir(path), key=lambda entry: entry.name.lower())
        except OSError:
            continue
        if any(child.name == ".git" for child in children):
            found.append({"name": path.name, "path": str(path), "active_at": _git_marker_time(path)})
            continue
        if depth >= max_depth:
            continue
        for child in children:
            if child.name.startswith(".") or child.name in SKIP_DISCOVERY:
                continue
            with contextlib.suppress(OSError):
                if child.is_dir(follow_symlinks=False) and not _is_within(Path(child.path), data_dir):
                    queue.append((Path(child.path), depth + 1))
    found.sort(key=lambda entry: entry["active_at"], reverse=True)
    return {"projects": found, "truncated": truncated}


def inspect_project(raw: str) -> dict:
    """Validate a project directory and describe its git state for the web console."""
    path = resolve_project_dir(raw)
    return {"path": str(path), **git_snapshot(path)}
