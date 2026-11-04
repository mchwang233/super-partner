"""Folder names a person can read: the date and time, then a few English words, never a random id
(``20261021-1441-async-fifo-uvm``), and folders the CLIs work in that lie outside any project."""
from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from .config import settings

MAX_WORDS = 6
MAX_CHARS = 48


def slug(text: Optional[str], default: str = "task") -> str:
    """Lowercase ASCII words joined by hyphens; ``default`` when ``text`` has none (Chinese only, say)."""
    words = re.findall(r"[a-z0-9]+", (text or "").lower())[:MAX_WORDS]
    return "-".join(words)[:MAX_CHARS].strip("-") or default


def reserve(root: Path, name: str) -> Path:
    """Create ``root/name``, or ``name-2``, ``name-3``... if taken, and return it."""
    root.mkdir(parents=True, exist_ok=True)
    for index in range(1, 1000):
        path = root / (name if index == 1 else f"{name}-{index}")
        try:
            path.mkdir()
            return path
        except FileExistsError:
            continue
    raise RuntimeError(f"no free folder name for {name} under {root}")


def task_folder(label: Optional[str], when: Optional[datetime] = None) -> str:
    """A new task's scratch folder under workspace_root, created now; returns its name."""
    stamp = (when or datetime.now().astimezone()).strftime("%Y%m%d-%H%M")  # local time
    return reserve(Path(settings.workspace_root), f"{stamp}-{slug(label)}").name


def lead_root() -> Path:
    """Where leads' conversations keep their folders: the setting, else the user's cache. Never
    inside the platform's own folder, so a CLI lead does not read its AGENTS.md or CLAUDE.md."""
    if settings.lead_root:
        return Path(settings.lead_root)
    cache = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(cache) / "super-partner" / "leads"


def lead_folder(model_id: str) -> Path:
    """A new folder for one lead conversation: when it started and on which model."""
    name = f"{datetime.now().astimezone():%Y%m%d-%H%M%S}-lead-{slug(model_id.split(':', 1)[-1], 'model')}"
    return reserve(lead_root(), name)
