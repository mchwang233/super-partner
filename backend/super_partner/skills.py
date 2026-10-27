"""Skills for the steps: how to use the tools a task needs, so a step does not learn them again.

A skill is a folder with a SKILL.md: front matter (``name``, ``description``, ``tools``, the
programs it is about) and Markdown. Two libraries:

- ``skills/steps/`` in the platform's source: what holds on any machine (how VCS is driven);
- ``<data_dir>/skills/``: what holds on this machine, written by the environment preparation
  (preflight.py) after trying the commands in a step's sandbox (which environment script, which
  licence, which options work here). These come first.

A step is given the skills whose tools its own text names (``for_step``).

Skills found in public repositories (skill_discovery.py) are kept apart, in
``<data_dir>/skills-discovered/`` (``discovered``): never given to a step, only shown to the model
that writes this machine's skill, as a reference it must check.
"""
from __future__ import annotations

import hashlib
import json
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
    source_url: str = ""
    source_revision: str = ""
    text: str = ""


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
                 body=text[match.end():].strip(), path=path, machine=machine, text=text)


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


def discovered(tools: Optional[list[str]] = None) -> list[Skill]:
    """Public skills cached from an earlier search, intact and from GitHub, about any of
    ``tools`` (all without it)."""
    wanted = None if tools is None else {tool.lower() for tool in tools}
    found = []
    for path in sorted((Path(settings.data_dir) / "skills-discovered").glob("*/SKILL.md")):
        try:
            text = path.read_text(encoding="utf-8")
            metadata = json.loads(path.with_name("source.json").read_text())
            url, revision = metadata.get("source_url"), metadata.get("source_revision")
            if (not isinstance(url, str) or not url.startswith("https://github.com/")
                    or not isinstance(revision, str) or not re.fullmatch(r"[a-f0-9]{40,64}", revision)
                    or hashlib.sha256(text.encode()).hexdigest() != metadata.get("sha256")):
                continue
            if skill := parse(text, path):
                skill.source_url, skill.source_revision = url, revision
                listed = metadata.get("tools")
                if not isinstance(listed, list) or not listed or not all(
                        isinstance(t, str) and re.fullmatch(r"[\w.+-]{2,64}", t) for t in listed):
                    continue
                skill.tools = listed
                found.append(skill)
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    return [skill for skill in found if wanted is None or wanted & {t.lower() for t in skill.tools}]


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


def save_discovered(skill: Skill) -> Path:
    """Cache public instructions separately from notes actually checked on this machine."""
    key = hashlib.sha256((skill.source_url + skill.source_revision).encode()).hexdigest()[:20]
    folder = Path(settings.data_dir) / "skills-discovered" / key
    folder.mkdir(parents=True, exist_ok=True)
    # Preserve upstream frontmatter (including licence/attribution), not just our parsed subset.
    text = skill.text or (f"---\nname: {skill.name}\ndescription: {skill.description}\n"
                          f"tools: [{', '.join(skill.tools)}]\n---\n{skill.body}\n")
    path = folder / "SKILL.md"
    path.write_text(text, encoding="utf-8")
    path.with_name("source.json").write_text(json.dumps({
        "source_url": skill.source_url, "source_revision": skill.source_revision,
        "sha256": hashlib.sha256(text.encode()).hexdigest(), "tools": skill.tools,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


# Lessons from earlier tasks (learning.py), written into this machine's skill for their tools,
# newest first, each between markers so that disabling one takes it out again.
LESSONS = "## Lessons from earlier tasks (checked here)"
MAX_LESSON_CHARS = 1500


def _marked(lesson_id: str) -> re.Pattern:
    return re.compile(rf"<!-- lesson:{re.escape(lesson_id)} -->\n.*?<!-- /lesson:{re.escape(lesson_id)} -->\n?",
                      re.DOTALL)


def add_lesson(lesson_id: str, title: str, text: str, tools: list[str]) -> list[Path]:
    """Write a checked lesson (a SKILL.md) into this machine's skills for its ``tools``; a new
    machine skill made of it when there is none. Returns the skills it is in now."""
    lesson = parse(text)
    body = (lesson.body if lesson else text).strip()
    if len(body) > MAX_LESSON_CHARS:
        body = body[:MAX_LESSON_CHARS] + "\n[...]"
    block = f"<!-- lesson:{lesson_id} -->\n### {title}\n{body}\n<!-- /lesson:{lesson_id} -->\n"
    wanted = {tool.lower() for tool in tools}
    targets = [skill for skill in library() if skill.machine and skill.path and wanted & set(skill.tools)]
    if not targets:
        name = lesson.name if lesson else (tools[0] if tools else lesson_id)
        description = lesson.description if lesson else title
        head = f"---\nname: {name}\ndescription: {description}\ntools: [{', '.join(tools)}]\n---\n# {name}\n"
        return [save_machine(name, f"{head}\n{LESSONS}\n\n{block}")]
    saved = []
    for skill in targets:
        current = _marked(lesson_id).sub("", skill.text)
        if LESSONS in current:
            current = current.replace(LESSONS + "\n\n", f"{LESSONS}\n\n{block}", 1)
        else:
            # Right after the front matter, so a long skill cut to fit a step's prompt keeps them.
            match = FRONT.match(current)
            at = match.end() if match else 0
            current = f"{current[:at]}{LESSONS}\n\n{block}\n{current[at:]}"
        skill.path.write_text(current)
        saved.append(skill.path)
    return saved


def remove_lesson(lesson_id: str) -> list[Path]:
    """Take a lesson out of this machine's skills again; returns those it was in."""
    import shutil
    changed = []
    for skill in library():
        if skill.machine and skill.path and _marked(lesson_id).search(skill.text):
            text = _marked(lesson_id).sub("", skill.text)
            if "<!-- lesson:" not in text:
                text = re.sub(rf"\n?{re.escape(LESSONS)}\n+", "\n", text, count=1)
            left = parse(text)
            if not left or left.body.strip() in {"", f"# {skill.name}"}:
                # A skill made of lessons alone, with none left: nothing to give a step.
                shutil.rmtree(skill.path.parent, ignore_errors=True)
            else:
                skill.path.write_text(text)
            changed.append(skill.path)
    return changed
