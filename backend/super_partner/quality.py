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
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".super-partner"}
VERDICT = re.compile(r"^\s*VERDICT:\s*(PASS|FAIL)\b:?\s*(.*)$", re.IGNORECASE | re.MULTILINE)


@dataclass
class Verdict:
    passed: bool
    reason: str
    fix: str = ""
    summary: Optional[str] = None
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


def node_prompt(node: TaskNode, run: ExecutionRun, limit: int) -> tuple[str, list[NodeInput]]:
    """The node's prompt and a record of what it received from each upstream node."""
    by_id = {item.id: item for item in run.plan.nodes}
    sections, inputs = [f"Task: {node.instructions}"], []
    if node.acceptance_criteria:
        sections.append(f"Acceptance criteria (your result will be checked against these):\n{node.acceptance_criteria}")
    if node.feedback:
        previous = f"\n\nYour previous result:\n{node.result[:limit]}" if node.result else ""
        sections.append(f"A previous attempt was not accepted. Fix these problems:\n{node.feedback}{previous}")
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
    if node.verifies:
        checked = ", ".join(by_id[item].title for item in node.verifies if item in by_id)
        sections.append(
            f"You are verifying: {checked}. Check the work against its requirements. End your answer "
            "with one final line, exactly `VERDICT: PASS` if it is acceptable, or "
            "`VERDICT: FAIL: <the specific problems to fix>`."
        )
    return "\n\n".join(sections), inputs


# --- checking a result ---------------------------------------------------------------------

def review_prompt(node: TaskNode, result: str, changed: list[str], diff: str, summarize: bool) -> str:
    criteria = node.acceptance_criteria or "The result fully and correctly completes the task."
    files = ", ".join(changed[:MAX_LISTED_FILES]) or "(none)"
    summary_rule = (
        ' Also include "summary": a self-contained summary of the result (at most 1500 characters) '
        "that later tasks can rely on instead of the full text, keeping concrete names, numbers and decisions."
        if summarize else ""
    )
    return (
        f"{REVIEW_MARKER}\nYou are reviewing another model's work. Do not modify any files.\n\n"
        f"Task:\n{node.instructions}\n\nAcceptance criteria:\n{criteria}\n\n"
        f"Result:\n{result}\n\nFiles changed: {files}\n"
        + (f"\nDiff of tracked files:\n{diff}\n" if diff else "")
        + '\nReply with JSON only: {"passed": true or false, "reason": "one or two sentences", '
        '"fix": "what to change if not passed, else empty"}.'
        + summary_rule
        + " Pass work that meets the criteria even if it could be polished; fail work that is "
        "wrong, incomplete, or only claims to have done something."
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
            ["git", "-C", str(root), "diff", "--no-color", "HEAD", "--", *files[:MAX_LISTED_FILES]],
            capture_output=True, text=True, timeout=20, check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    if len(output) > MAX_DIFF_CHARS:
        output = output[:MAX_DIFF_CHARS] + f"\n[... diff clipped, {len(output) - MAX_DIFF_CHARS} more characters]"
    return output


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
