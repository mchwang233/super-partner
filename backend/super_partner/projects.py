from __future__ import annotations

import subprocess
from pathlib import Path

from .config import settings

MAX_CONTEXT_ENTRIES = 60


class ProjectDirError(ValueError):
    pass


def resolve_project_dir(raw: str) -> Path:
    """Validate a user-supplied project directory and return its canonical path."""
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ProjectDirError("Project directory must be an absolute path")
    path = path.resolve()
    if not path.is_dir():
        raise ProjectDirError(f"Project directory does not exist: {path}")
    roots = [root.expanduser().resolve() for root in settings.project_roots]
    if not any(path == root or root in path.parents for root in roots):
        allowed = ", ".join(str(root) for root in roots)
        raise ProjectDirError(f"Project directory must be inside an allowed root: {allowed}")
    data_dir = settings.data_dir.resolve()
    if path == data_dir or data_dir in path.parents:
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
