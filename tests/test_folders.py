"""Folders a person can read, and leads' folders outside any project (folders.py)."""
import json
import re
from datetime import datetime, timezone

from super_partner import folders, outline, supervisor
from super_partner.config import settings
from test_turns import model


def test_names_are_english_words_and_the_time():
    assert folders.slug("Async FIFO: VCS/UVM verification & coverage!") == "async-fifo-vcs-uvm-verification-coverage"
    assert folders.slug("写一个通用异步fifo") == "fifo" and folders.slug("写一个命令行工具") == "task"
    name = folders.task_folder("async-fifo-uvm", datetime(2026, 10, 21, 14, 41, tzinfo=timezone.utc))
    assert name == "20261021-1441-async-fifo-uvm" and (settings.workspace_root / name).is_dir()
    # The same name twice in a minute: the second gets a number.
    assert folders.task_folder("async-fifo-uvm", datetime(2026, 10, 21, 14, 41, tzinfo=timezone.utc)) == "20261021-1441-async-fifo-uvm-2"


def test_a_lead_works_outside_the_platforms_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "lead_root", None)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    session = supervisor.new_session(model("cli:codex", 95, "cli"))
    folder = tmp_path / "cache" / "super-partner" / "leads"
    assert session.cwd.startswith(str(folder)) and re.fullmatch(r"\d{8}-\d{6}-lead-codex", session.cwd.rsplit("/", 1)[-1])
    assert not session.cwd.startswith(str(settings.data_dir))


async def test_the_outline_names_the_tasks_folder():
    async def say(message):
        return json.dumps({"summary": "异步 FIFO", "folder": "async-fifo-uvm", "parts": [
            {"key": "p1", "title": "RTL", "goal": "g", "provides": "rtl/fifo.v", "size": 2}]})
    assert (await outline.plan_outline(say, "")).folder == "async-fifo-uvm"
