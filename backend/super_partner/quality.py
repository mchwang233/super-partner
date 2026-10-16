"""Checking node results and handing them on: acceptance review, verification verdicts,
upstream handoff, and the files each CLI node changed."""
from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .models import ExecutionRun, NodeInput, TaskNode

# Marks a review request so the offline demo model can answer it with a verdict.
REVIEW_MARKER = "[[super-partner:review]]"
MAX_LISTED_FILES = 50
MAX_DIFF_CHARS = 12_000
MAX_SCANNED_FILES = 20_000
MAX_INVENTORY_FILES = 200
# Relative or absolute paths, or bare file names with an extension, mentioned in a result.
PATH_MENTION = re.compile(r"/?[\w.-]+(?:/[\w.-]+)+|[\w-]+\.[A-Za-z0-9]{1,8}\b")
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".super-partner"}
# Explicitly cited verification logs may live in hidden scratch folders. This exception
# never grants access to credentials, agent state or repository metadata.
PRIVATE_DIRS = {".ssh", ".aws", ".azure", ".config", ".kube", ".docker", ".gnupg",
                ".codex", ".claude", ".agents", ".review", "credentials", "secrets"}
PRIVATE_NAMES = {"credentials", "secrets", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"}
PRIVATE_SUFFIXES = {".key", ".pem", ".p12", ".pfx", ".keystore", ".db", ".sqlite", ".sqlite3"}
LOG_SUFFIXES = {".log", ".txt", ".out"}
# A worker's own statement that part of its task cannot be done in this environment.
UNABLE = re.compile(r"^\s*UNABLE:\s*(.+)$", re.IGNORECASE | re.MULTILINE)
UNABLE_RULE = (
    "Be honest about what actually happened. If something this step's own task requires cannot "
    "really be done here (no network access, missing credentials, accounts, permissions, hardware or services), "
    "do not simulate it, fake its output, or describe it as done: do every part you can, say what "
    "is ready, and end your answer with one final line `UNABLE: <what could not be done, why, and "
    "what would make it possible>`. Judge only this step: work that belongs to other steps or to "
    "the overall goal (such as deploying, when this step only builds) is not yours to do or report. "
    "Running out of turns or time is not a limit of the environment: report your progress instead."
)
VERDICT = re.compile(r"^\s*VERDICT:\s*(PASS|FAIL)\b:?\s*(.*)$", re.IGNORECASE | re.MULTILINE)
# Feedback a failed verification sends back, and what later retries append to it.
REWORK_PREFIX = "验证（"
RETRY_NOTE = "\n本次验收意见："
ADVICE_NOTE = "\n诊断建议："


@dataclass
class Verdict:
    passed: bool
    reason: str
    fix: str = ""
    summary: Optional[str] = None
    # Set when the task's goal cannot be reached in this environment, with why.
    unable: Optional[str] = None
    reviewer_model_id: Optional[str] = None
    cost_usd: float = 0.0
    accounted_cost_usd: float = 0.0
    verified: bool = True


# --- what a node is told -------------------------------------------------------------------

def _handoff(item: TaskNode, limit: int) -> tuple[str, str]:
    result = item.result or ""
    if len(result) <= limit:
        return result, "full"
    if item.summary:
        return item.summary, "summary"
    return result[:limit] + f"\n[... {len(result) - limit} more characters omitted]", "truncated"


# Titles of the task's other steps a node is shown.
MAX_OTHER_STEPS = 30
# Background on the earlier task that each step of a requirement change gets.
BACKGROUND_CHARS = 2500


def node_prompt(node: TaskNode, run: ExecutionRun, limit: int) -> tuple[str, list[NodeInput]]:
    """The node's prompt and a record of what it received from each upstream node."""
    by_id = {item.id: item for item in run.plan.nodes}
    sections, inputs = [f"Task: {node.instructions}"], []
    if run.background:
        # A requirement change: the working directory already holds the earlier task's results.
        sections.append("Context: this task changes an earlier task. Its results are already in the working "
                        "directory; build on them and keep what the change does not touch.\n"
                        + run.background[:BACKGROUND_CHARS])
    if node.acceptance_criteria:
        sections.append(f"Acceptance criteria (your result will be checked against these):\n{node.acceptance_criteria}")
    if node.feedback:
        previous = f"\n\nYour previous result:\n{node.result[:limit]}" if node.result else ""
        sections.append(f"A previous attempt was not accepted. Fix these problems:\n{node.feedback}{previous}")
    replied = sorted((d for d in run.diagnoses if d.messages), key=lambda d: d.messages[-1].created_at)
    if replied:
        clarifications = []
        for diagnosis in replied[-5:]:
            for message in diagnosis.messages[-3:]:
                clarifications.append(
                    f"Question: {diagnosis.question or 'Additional instructions'}\n"
                    f"User's choice: {message.choice or '(free text)'}\nUser's reply: {message.text}"
                )
        sections.append("User clarifications during diagnosis; preserve these when continuing the original task:\n"
                        + "\n\n".join(clarifications))
    upstream = []
    for dependency in node.dependencies:
        item = by_id.get(dependency)
        if not item or not item.result:
            continue
        text, form = _handoff(item, limit)
        files = item.changed_files[:MAX_LISTED_FILES]
        listed = f"\nFiles it changed: {', '.join(files)}" if files else ""
        if len(item.changed_files) > len(files):
            listed += f" (and {len(item.changed_files) - len(files)} more)"
        label = {"full": "result", "summary": "summary of its result", "truncated": "start of its result"}[form]
        upstream.append(f"### {item.title} ({label})\n{text}{listed}")
        inputs.append(NodeInput(node_id=item.id, form=form, changed_files=len(item.changed_files)))
    sections.append("Results from earlier tasks:\n" + ("\n\n".join(upstream) if upstream else "(none)"))
    others = [item.title for item in run.plan.nodes if item.id != node.id]
    if others:
        # So a step knows what is someone else's job, and neither does it nor reports it undone.
        sections.append("Other steps of this task, not yours: " + "; ".join(others[:MAX_OTHER_STEPS]))
    sections.append(UNABLE_RULE)
    if node.verifies:
        checked = ", ".join(by_id[item].title for item in node.verifies if item in by_id)
        sections.append(
            f"You are verifying: {checked}. Check the work against its requirements. End your answer "
            "with one final line, exactly `VERDICT: PASS` if it is acceptable, or "
            "`VERDICT: FAIL: <the specific problems to fix>`."
        )
    return "\n\n".join(sections), inputs


def rework_feedback(feedback: Optional[str]) -> str:
    """The rework a failed verification asked for, without notes later retries appended."""
    if not feedback or not feedback.startswith(REWORK_PREFIX):
        return ""
    for note in (RETRY_NOTE, ADVICE_NOTE):
        feedback = feedback.split(note, 1)[0]
    return feedback.strip()


def retry_feedback(previous: Optional[str], problem: str) -> str:
    """Feedback for a retry: a rejection must not erase the rework the attempt was doing,
    or the next attempt undoes it and the verification sends it back again."""
    rework = rework_feedback(previous)
    return f"{rework}{RETRY_NOTE}{problem}" if rework else problem


# --- checking a result ---------------------------------------------------------------------

def review_prompt(node: TaskNode, result: str, changed: list[str], diff: str, summarize: bool) -> str:
    criteria = node.acceptance_criteria or "The result fully and correctly completes the task."
    files = ", ".join(changed[:MAX_LISTED_FILES]) or "(none)"
    summary_rule = (
        ' Also include "summary": a self-contained summary of the result (at most 1500 characters) '
        "that later tasks can rely on instead of the full text, keeping concrete names, numbers and decisions."
        if summarize else ""
    )
    rework = (
        "Rework requirements this attempt was given (these supersede conflicting parts of the original "
        "task; where they conflict, judge by the rework requirements):\n"
        f"{node.feedback}\n\n" if node.feedback else ""
    )
    return (
        f"{REVIEW_MARKER}\nYou are reviewing another model's work. Do not modify any files.\n\n"
        f"Task:\n{node.instructions}\n\nAcceptance criteria:\n{criteria}\n\n{rework}"
        f"Result:\n{result}\n\nFiles changed: {files}\n"
        + (
            "\nEvidence from the workspace (diff or contents of the relevant files, then a listing of the "
            "files that exist). Files this attempt did not change may come from earlier attempts; judge "
            "them by these contents and the listing, not by the list of changed files. Excerpts may "
            "be clipped: omitted content is not evidence that an implementation or check is missing. "
            "When read_file is available, inspect the relevant files before deciding; use tail=true "
            f"to read the final verification output in a long log:\n{diff}\n"
            if diff else ""
        )
        + '\nReply with JSON only: {"passed": true or false, "reason": "one or two sentences", '
        '"fix": "what to change if not passed, else empty", '
        '"unable": "empty, or what the task required that did not actually happen because of a limit '
        'outside the work, why, and what would make it possible"}.'
        + summary_rule
        + " Pass work that meets the criteria even if it could be polished; fail work that is "
        "wrong, incomplete, or only claims to have done something."
        " You run in a restricted sandbox that may have no network and may not even open loopback "
        "sockets. Checks of yours that fail for that reason (PermissionError, socket or network "
        "operations not permitted) are a limit of your environment, not a defect of the work: never "
        "fail the work for them; judge by the code, the output reported in the result, and whatever "
        "you can run offline."
        " Judge the outcome this task asks for, not the preparation, and only this task: work it "
        "does not ask for (deploying, when it only asks to build) neither fails it nor is unable."
        " If it requires an effect outside "
        "the files (deploying, publishing, uploading, sending, installing on a server, calling an "
        "external service) and the result shows it did not actually happen, was simulated, or was "
        "only prepared (\"scripts are ready\", \"would work with network access\"), the task is not "
        "done: set passed to false, and when the cause is a limit of the environment (no network, "
        "missing credentials or permissions) rather than a mistake, describe it in \"unable\"."
    )


def parse_review(text: str) -> Optional[dict]:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        payload = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("passed"), bool):
        return None
    return payload


def unable_claim(text: str) -> Optional[str]:
    """The last UNABLE line of a result: what its model says cannot be done here."""
    matches = UNABLE.findall(text or "")
    return matches[-1].strip()[:1000] if matches else None


def verification_verdict(text: str) -> Optional[tuple[bool, str]]:
    """The last VERDICT line of a verification node, or None when it gave none."""
    matches = VERDICT.findall(text or "")
    if not matches:
        return None
    outcome, problems = matches[-1]
    return outcome.upper() == "PASS", problems.strip()


# --- files a CLI node changed --------------------------------------------------------------

def file_state(root: Path) -> dict[str, tuple[int, int]]:
    """Modification time and size of the files under ``root``, skipping build and VCS folders."""
    state: dict[str, tuple[int, int]] = {}
    if not root.is_dir():
        return state
    for folder, dirs, files in os.walk(root):
        dirs[:] = [name for name in dirs if name not in SKIP_DIRS]
        for name in files:
            path = Path(folder, name)
            try:
                info = path.stat()
            except OSError:
                continue
            state[path.relative_to(root).as_posix()] = (info.st_mtime_ns, info.st_size)
            if len(state) >= MAX_SCANNED_FILES:
                return state
    return state


def changed_files(before: dict, after: dict) -> list[str]:
    return sorted(path for path in before.keys() | after.keys() if before.get(path) != after.get(path))


def git_diff(root: Path, files: list[str]) -> str:
    """Diff of the changed tracked files, clipped, so a reviewer sees what actually changed."""
    if not files or not (root / ".git").exists():
        return ""
    try:
        output = subprocess.run(
            ["git", "--literal-pathspecs", "-C", str(root), "diff", "--no-color", "--no-ext-diff",
             "--no-textconv", "--no-renames", "HEAD", "--", *files[:MAX_LISTED_FILES]],
            capture_output=True, text=True, timeout=20, check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    return _excerpt(output, MAX_DIFF_CHARS)


def _tracked(root: Path, files: list[str]) -> set[str]:
    if not files or not (root / ".git").exists():
        return set()
    try:
        output = subprocess.run(
            ["git", "--literal-pathspecs", "-C", str(root), "ls-files", "-z", "--", *files],
            capture_output=True, text=True, timeout=20, check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return set()
    return {name for name in output.split("\0") if name}


def review_path(root: Path, name: str, *, allow_hidden_log: bool = False) -> Optional[Path]:
    """One access policy for inline evidence and API review tools, including symlink targets.

    Only an explicitly referenced log may bypass the normal hidden-folder exclusion.
    Both its written path and resolved target must be safe and inside the workspace.
    """
    try:
        base = root.resolve()
        path = base / name
        resolved = path.resolve()
        for relative in (path.relative_to(base), resolved.relative_to(base)):
            parts = relative.parts
            for i, part in enumerate(parts):
                lower = part.lower()
                stem = Path(lower).stem
                if (lower in SKIP_DIRS or lower in PRIVATE_DIRS or lower in PRIVATE_NAMES
                        or lower.startswith((".env", "id_rsa.", "id_dsa.", "id_ecdsa.", "id_ed25519."))
                        or stem in {"credentials", "secrets", "token", "tokens"}
                        and Path(lower).suffix in {".json", ".yaml", ".yml", ".toml", ".ini", ".txt", ".log"}
                        or Path(lower).suffix in PRIVATE_SUFFIXES):
                    return None
                if part.startswith(".") and not (
                    allow_hidden_log and i < len(parts) - 1 and part not in {".", ".."}
                    and relative.suffix.lower() in LOG_SUFFIXES
                ):
                    return None
        return resolved
    except (OSError, ValueError, RuntimeError):
        return None


def _excerpt(text: str, limit: int) -> str:
    """Keep both ends: verification summaries are usually at the end of a file."""
    if len(text) <= limit:
        return text
    note = "\n[... clipped; read_file can inspect omitted content ...]\n"
    if limit <= len(note):
        return note[:max(0, limit)]
    head = (limit - len(note) + 1) // 2
    tail = limit - len(note) - head
    return text[:head] + note + (text[-tail:] if tail else "")


def _file_text(root: Path, name: str, limit: int, *, allow_hidden_log: bool = False) -> Optional[str]:
    """Bounded head/tail text excerpts, or None for private, binary or unreadable files."""
    path = review_path(root, name, allow_hidden_log=allow_hidden_log)
    if path is None:
        return None
    try:
        if not path.exists():
            return "(deleted)"
        if not path.is_file():
            return None
        with path.open("rb") as handle:
            # Four bytes cover a UTF-8 character; never load a whole large verification log.
            size = max(1, limit) * 4
            data = handle.read(size + 1)
            if len(data) > size:
                handle.seek(-size, os.SEEK_END)
                end = handle.read(size)
                if b"\0" in data or b"\0" in end:
                    return None
                # The byte ranges can cut through a multi-byte character at either end.
                text = data.decode("utf-8", errors="replace") + end.decode("utf-8", errors="replace")
            else:
                if b"\0" in data:
                    return None
                text = data.decode("utf-8")
        return _excerpt(text, limit)
    except (OSError, UnicodeDecodeError):
        return None


def _skipped(parts) -> bool:
    return any(part in SKIP_DIRS or part.startswith(".") for part in parts)


def mentioned_files(root: Path, text: str) -> list[str]:
    """Workspace files named in a result, relative to ``root``: the evidence for an attempt
    that checked earlier work without changing anything."""
    found: list[str] = []
    try:
        base = root.resolve()
    except OSError:
        return found
    for match in PATH_MENTION.findall(text or ""):
        name = match.rstrip(".")
        if name.startswith("/"):
            try:
                name = Path(name).relative_to(base).as_posix()
            except ValueError:
                continue
        name = name.removeprefix("./")
        if not name or name in found:
            continue
        path = review_path(root, name, allow_hidden_log=True)
        try:
            if path is not None and path.is_file():
                found.append(name)
        except OSError:
            continue
        if len(found) >= MAX_LISTED_FILES:
            break
    return found


def workspace_listing(root: Path) -> str:
    """Public workspace files with their sizes, including unchanged files."""
    entries: list[str] = []
    if not root.is_dir():
        return ""
    for folder, dirs, files in os.walk(root):
        dirs[:] = sorted(name for name in dirs if not _skipped([name])
                         and review_path(root, str(Path(folder, name).relative_to(root))) is not None)
        for name in sorted(files):
            path = Path(folder, name)
            if review_path(root, path.relative_to(root).as_posix()) is None:
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            entries.append(f"{path.relative_to(root).as_posix()} ({size} bytes)")
            if len(entries) > MAX_INVENTORY_FILES:
                entries[-1] = "[... more files not listed]"
                return "\n".join(entries)
    return "\n".join(entries)


def change_evidence(root: Path, files: list[str], *, referenced: list[str] = ()) -> str:
    """Relevant file excerpts sharing a bounded budget, followed by a workspace listing.

    Changed files, earlier work and explicit references all contribute evidence. A long
    delivery document must not crowd out source, tests or the final verification output.
    """
    allowed = set(referenced)
    files = [name for name in dict.fromkeys(files)
             if review_path(root, name, allow_hidden_log=name in allowed) is not None]
    # Prefer implementation/tests over logs and prose when there are too many files to inline.
    files.sort(key=lambda name: 2 if Path(name).suffix.lower() in {".md", ".rst", ".txt"}
               else 1 if Path(name).suffix.lower() in LOG_SUFFIXES else 0)
    output = _changed_contents(root, files[:MAX_LISTED_FILES], allowed)
    listing = workspace_listing(root)
    if listing:
        output = (output + "\n" if output else "") + f"--- files in the workspace ---\n{listing}"
    return output


def _changed_contents(root: Path, files: list[str], referenced: set[str]) -> str:
    if not files:
        return ""
    tracked = _tracked(root, files)
    sections = []
    remaining = MAX_DIFF_CHARS
    for index, name in enumerate(files):
        # Reserve a share for every remaining file, redistributing unused space from short ones.
        budget = remaining // (len(files) - index)
        diff = git_diff(root, [name]) if name in tracked else ""
        header = f"--- {name} ({'diff' if diff else 'contents'}) ---\n"
        limit = max(0, budget - len(header) - 1)
        text = _excerpt(diff, limit) if diff else _file_text(
            root, name, limit, allow_hidden_log=name in referenced,
        )
        if text is not None:
            section = _excerpt(header + text, max(0, budget - 1))
            sections.append(section)
            remaining -= len(section) + 1
    return "\n".join(sections)


def descendants(run: ExecutionRun, roots: set[str]) -> set[str]:
    """Nodes that depend, directly or not, on any of ``roots``."""
    found: set[str] = set()
    changed = True
    while changed:
        changed = False
        for node in run.plan.nodes:
            if node.id not in found and (set(node.dependencies) & (roots | found)):
                found.add(node.id)
                changed = True
    return found
