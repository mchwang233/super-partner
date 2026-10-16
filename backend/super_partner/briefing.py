"""The project brief: what each file of a working directory is for, written once by a cheap
model and kept current, so that every step, whichever model runs it, starts knowing the project
instead of reading it again. Only files whose content changed are summarized again; the brief
is kept per working directory under the data folder and reused by later tasks.

The brief is for whoever plans (``planning_brief``): with it the lead cuts the task into small
work packages that each name their files, and each step then gets only the full text of those
files (``attached_files``), not the project.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Optional

from .agent import BINARY_SUFFIXES, project_map
from .models import ExecutionRun, TaskNode
from .quality import SKIP_DIRS, review_path

logger = logging.getLogger(__name__)

# Files worth a summary: source, configuration and documentation, not logs or data dumps.
SUMMARIZED = {".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".vue", ".svelte", ".css", ".scss", ".html",
              ".md", ".rst", ".sh", ".bash", ".yml", ".yaml", ".toml", ".ini", ".cfg", ".conf", ".service",
              ".sql", ".go", ".rs", ".java", ".kt", ".c", ".h", ".cpp", ".hpp", ".cs", ".rb", ".php", ".swift",
              ".v", ".sv", ".vhd", ".tcl", ".r", ".lua", ".dockerfile", ".example", ".json"}
SUMMARIZED_NAMES = {"Dockerfile", "Makefile", "Procfile", "requirements.txt", "package.json", "pyproject.toml"}
MAX_SUMMARIZED_BYTES = 200_000
MAX_SMALL_JSON = 20_000
MAX_SUMMARIZED_FILES = 300
# What a summarizing request carries: whole files up to this much text, in this many at a time.
BATCH_CHARS = 120_000
FILE_CHARS = 40_000
PARALLEL = 4
# Full text attached to a step.
ATTACH_CHARS = 100_000
ATTACH_FILE_CHARS = 64_000
MAX_ATTACHED = 13
# A scoped work package gets all its files, however long each is, within this.
SCOPED_ATTACH_CHARS = 200_000

Ask = Callable[[str], Awaitable[str]]


def _summarized(root: Path, path: Path) -> bool:
    relative = path.relative_to(root)
    if any(part.startswith(".") or part in SKIP_DIRS for part in relative.parts):
        return False
    suffix = path.suffix.lower() or ("." + path.name.lower() if path.name.lower() == "dockerfile" else "")
    if path.name not in SUMMARIZED_NAMES and suffix not in SUMMARIZED:
        return False
    try:
        size = path.stat().st_size
    except OSError:
        return False
    if size > (MAX_SMALL_JSON if suffix == ".json" else MAX_SUMMARIZED_BYTES):
        return False
    # Never send credentials or private files to a model.
    return review_path(root, relative.as_posix()) is not None


def scan(root: Path) -> dict[str, tuple[str, str]]:
    """{path: (content hash, text)} of the files worth a summary."""
    found: dict[str, tuple[str, str]] = {}
    for folder, dirs, names in os.walk(root):
        dirs[:] = sorted(name for name in dirs if name not in SKIP_DIRS and not name.startswith("."))
        for name in sorted(names):
            path = Path(folder, name)
            if path.suffix.lower() in BINARY_SUFFIXES or not _summarized(root, path):
                continue
            try:
                data = path.read_bytes()
            except OSError:
                continue
            if b"\0" in data[:4096]:
                continue
            found[path.relative_to(root).as_posix()] = (hashlib.sha1(data).hexdigest(),
                                                        data.decode("utf-8", errors="replace"))
            if len(found) >= MAX_SUMMARIZED_FILES:
                return found
    return found


async def planning_brief(root: Path, data_dir: Path, providers, writer) -> tuple[str, float]:
    """The brief of ``root`` for planning, brought up to date by ``writer`` (a ModelProfile, or
    None for just the file list), and what writing it cost."""
    spent = 0.0

    async def ask(text: str) -> str:
        nonlocal spent
        result = await providers.complete(writer.id, text, effort="low")
        spent += (result.input_tokens * (writer.input_per_million or 0)
                  + result.output_tokens * (writer.output_per_million or 0)) / 1_000_000
        return result.text

    brief = None
    if writer:
        try:
            brief = await refresh(root, data_dir, ask)
        except Exception as exc:
            logger.warning("project brief unavailable for planning: %s", exc)
    return await asyncio.to_thread(render, root, brief), spent


def store_path(data_dir: Path, root: Path) -> Path:
    return data_dir / "briefs" / f"{hashlib.sha1(str(root.resolve()).encode()).hexdigest()[:16]}.json"


def load(path: Path) -> dict:
    try:
        brief = json.loads(path.read_text())
        return brief if isinstance(brief, dict) and isinstance(brief.get("files"), dict) else {"files": {}}
    except (OSError, ValueError):
        return {"files": {}}


SUMMARY_PROMPT = """You are writing a project brief that other models read instead of opening every file.
For each file below, write a dense summary in English of at most 3 lines: what it is for, and its
main contents (functions, classes, endpoints, commands, settings or sections, by name) so a reader
knows whether to open it. Do not describe code style. Answer with one JSON object only, mapping each
file path exactly as given to its summary.

"""

OVERVIEW_PROMPT = """You are writing the overview of a project brief that other models read before working on
the project. From the file list and summaries below, write at most 25 lines in English: what the
project is, how it is organised, how to run it and its tests (exact commands when they can be
inferred), and conventions worth following. Answer with the overview text only.

"""


def _batches(texts: dict[str, str]) -> list[dict[str, str]]:
    batches, current, size = [], {}, 0
    for path, text in texts.items():
        text = text[:FILE_CHARS]
        if current and size + len(text) > BATCH_CHARS:
            batches.append(current)
            current, size = {}, 0
        current[path], size = text, size + len(text)
    return batches + ([current] if current else [])


def _parse(text: str, paths) -> dict[str, str]:
    match = re.search(r"\{.*\}", text or "", re.DOTALL)
    try:
        data = json.loads(match.group(0)) if match else {}
    except ValueError:
        return {}
    return {path: str(data[path]).strip()[:600] for path in paths if isinstance(data, dict) and data.get(path)}


async def refresh(root: Path, data_dir: Path, ask: Ask) -> dict:
    """The brief of ``root``, with the files that are new or changed since it was written
    summarized again and the files that are gone dropped. A summary that fails is left out
    (and tried again next time) rather than failing the step."""
    path = store_path(data_dir, root)
    brief = load(path)
    current = await asyncio.to_thread(scan, root)
    files = {name: entry for name, entry in brief["files"].items() if name in current}
    stale = {name: text for name, (sha, text) in current.items()
             if files.get(name, {}).get("sha") != sha}
    changed = bool(stale) or len(files) != len(brief["files"])
    if stale:
        limit = asyncio.Semaphore(PARALLEL)

        async def summarize(batch):
            async with limit:
                prompt = SUMMARY_PROMPT + "\n\n".join(f"=== {name} ===\n{text}" for name, text in batch.items())
                try:
                    return _parse(await ask(prompt), batch)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # a missing summary only costs a read later
                    logger.warning("project brief summaries failed: %s", exc)
                    return {}

        for summaries in await asyncio.gather(*(summarize(batch) for batch in _batches(stale))):
            for name, summary in summaries.items():
                files[name] = {"sha": current[name][0], "summary": summary}
    if files and (not brief.get("overview") or len(stale) > max(3, len(current) // 5)):
        listing = "\n".join(f"{name}: {entry['summary']}" for name, entry in sorted(files.items()))
        try:
            overview = re.sub(r"^```\w*\n|\n?```$", "", (await ask(OVERVIEW_PROMPT + listing)).strip())
            brief["overview"] = overview.strip()[:4000]
            changed = True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("project brief overview failed: %s", exc)
    brief["files"] = files
    if changed:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(brief, ensure_ascii=False))
    return brief


BRIEF_NOTE = (
    "Project brief, written from the files' current contents so you need not open every file to "
    "find your way. Use it to decide what to read; read a file before you edit it unless its full "
    "text is attached below."
)


def render(root: Path, brief: Optional[dict]) -> str:
    """The brief as given to a step: the overview, then every file with its length and summary."""
    summaries = {name: entry["summary"] for name, entry in ((brief or {}).get("files") or {}).items()}
    files = project_map(root, summaries)
    if not summaries:
        return files
    overview = (brief or {}).get("overview")
    return "\n\n".join(part for part in [BRIEF_NOTE, f"Overview:\n{overview}" if overview else "", files] if part)


PATH_LIKE = re.compile(r"[\w./-]*[\w-]\.[A-Za-z0-9]{1,8}\b")


def likely_files(root: Path, node: TaskNode, run: ExecutionRun) -> list[str]:
    """The files a step the planner did not scope most likely needs: those its instructions
    name, then those the steps it depends on changed."""
    text = f"{node.instructions}\n{node.acceptance_criteria}"
    wanted, names = [], None
    for token in PATH_LIKE.findall(text):
        token = token.strip("./")
        if (root / token).is_file():
            wanted.append(token)
        elif "/" not in token and re.search(r"\.[A-Za-z]", token):
            # A bare file name: the file of that name, if only one has it.
            if names is None:
                names = {}
                for folder, dirs, files in os.walk(root):
                    dirs[:] = [name for name in dirs if name not in SKIP_DIRS and not name.startswith(".")]
                    for name in files:
                        names.setdefault(name, []).append(Path(folder, name).relative_to(root).as_posix())
            if len(names.get(token, [])) == 1:
                wanted.append(names[token][0])
    by_id = {item.id: item for item in run.plan.nodes}
    for dependency in node.dependencies:
        wanted += by_id[dependency].changed_files if dependency in by_id else []
    return list(dict.fromkeys(wanted))


def attached_files(root: Path, names: list[str], total_chars: int = ATTACH_CHARS,
                   file_chars: int = ATTACH_FILE_CHARS) -> str:
    """Full text of the files ``names`` that exist, in order, while they fit: no file longer than
    ``file_chars``, none past ``total_chars`` in all. Never binary, hidden or private files."""
    sections, total = [], 0
    for name in dict.fromkeys(names):
        path = root / name
        if (len(sections) >= MAX_ATTACHED or not path.is_file() or path.suffix.lower() in BINARY_SUFFIXES
                or review_path(root, name) is None):
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if b"\0" in data[:4096] or len(data) > file_chars or total + len(data) > total_chars:
            continue
        total += len(data)
        sections.append(f"=== {name} (full text, current) ===\n{data.decode('utf-8', errors='replace')}")
    if not sections:
        return ""
    return ("Files attached in full, as they are now; do not read them again unless they change:\n\n"
            + "\n\n".join(sections))
