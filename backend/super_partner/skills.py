"""Skills for the steps: how to use the tools a task needs, so a step does not learn them again.

A skill is a folder with a SKILL.md: front matter (``name``, ``description``, ``tools``, the
programs it is about) and Markdown. Two libraries:

- ``skills/steps/`` in the platform's source: what holds on any machine (how VCS is driven);
- ``<data_dir>/skills/``: what holds on this machine, written by the environment preparation
  (preflight.py) after trying the commands in a step's sandbox (which environment script, which
  licence, which options work here). These come first.

A step is given the skills whose tools its own text names (``for_step``).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .config import settings

SOURCE = Path(__file__).resolve().parents[2] / "skills" / "steps"
# How much of one skill a step's prompt carries, and how many skills.
MAX_SKILL_CHARS = 7000
MAX_SKILLS = 3
FRONT = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)


@dataclass
class Skill:
    name: str
    description: str
    tools: list[str] = field(default_factory=list)
    body: str = ""
    path: Optional[Path] = None
    machine: bool = False


def parse(text: str, path: Optional[Path] = None, machine: bool = False) -> Optional[Skill]:
    """A SKILL.md, or None if it has no front matter with a name."""
    match = FRONT.match(text or "")
    if not match:
        return None
    fields = {}
    for line in match.group(1).splitlines():
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    name = fields.get("name", "").strip()
    if not name:
        return None
    tools = [item.strip().strip("'\"").lower() for item in fields.get("tools", "").strip("[]").split(",")]
    return Skill(name=name, description=fields.get("description", ""), tools=[item for item in tools if item],
                 body=text[match.end():].strip(), path=path, machine=machine)


def machine_dir() -> Path:
    return Path(settings.data_dir) / "skills"


def library(source: Optional[Path] = None) -> list[Skill]:
    """This machine's skills first, then the platform's."""
    found = []
    for root, machine in ((machine_dir(), True), (source or SOURCE, False)):
        for path in sorted(root.glob("*/SKILL.md")) if root.is_dir() else []:
            try:
                skill = parse(path.read_text(errors="replace"), path, machine)
            except OSError:
                continue
            if skill:
                found.append(skill)
    return found


def names_tool(text: str, tool: str) -> bool:
    return re.search(rf"(?<![\w./-]){re.escape(tool)}(?![\w-])", text, re.IGNORECASE) is not None


def for_tools(tools: list[str], skills: Optional[list[Skill]] = None) -> list[Skill]:
    """The skills about any of ``tools``."""
    wanted = {tool.lower() for tool in tools}
    return [skill for skill in (library() if skills is None else skills) if wanted & set(skill.tools)]


def for_step(text: str, skills: Optional[list[Skill]] = None) -> list[Skill]:
    """The skills whose tools ``text`` (a step's title, instructions, check) names, this
    machine's before the platform's, at most MAX_SKILLS."""
    chosen = [skill for skill in (library() if skills is None else skills)
              if any(names_tool(text, tool) for tool in skill.tools)]
    return chosen[:MAX_SKILLS]


def section(skills: list[Skill]) -> str:
    """The skills as a part of a step's prompt."""
    if not skills:
        return ""
    parts = []
    for skill in skills:
        body = skill.body if len(skill.body) <= MAX_SKILL_CHARS else skill.body[:MAX_SKILL_CHARS] + "\n[...]"
        origin = "checked on this machine" if skill.machine else "general"
        parts.append(f"### Skill: {skill.name} ({origin})\n{body}")
    return ("Skills for the tools this step uses. Follow them rather than finding out how the tools work "
            "again; where this machine's notes and the general skill differ, this machine's are right.\n\n"
            + "\n\n".join(parts))


def save_machine(tool: str, text: str) -> Path:
    """Keep a skill written on this machine for ``tool``; returns its path."""
    folder = machine_dir() / re.sub(r"[^a-z0-9-]+", "-", tool.lower()).strip("-")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "SKILL.md"
    path.write_text(text.rstrip() + "\n")
    return path
