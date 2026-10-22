"""Looking back on finished tasks, so problems that keep coming back are seen as such.

When a task ends, ``record`` sums it up from what was recorded, without asking a model: each
step's estimate against what it cost and the turns it took, and the problems it ran into, each
named by a kind and a key (``("environment", "vcs")``, ``("model_unavailable", "glm:glm-5.3")``)
so the same problem in different tasks has the same name. ``recurring`` counts each problem
across tasks and marks the ones that came back after a repair; ``similar`` gives a diagnosis
the earlier cases of the problem in front of it (diagnosis.repair_prompt).
"""
from __future__ import annotations

import logging
import re
from collections import Counter
from typing import Optional

from .models import DiagnosisStatus, ExecutionRun, Outcome, RunStatus
from .routing import loop_turns

logger = logging.getLogger(__name__)

FINISHED = {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.INCOMPLETE}
END_EVENTS = {"run.succeeded", "run.failed", "run.incomplete", "run.cancelled"}
# A step's cost or turns this many times its estimate is a problem worth counting.
OVER = 2.0
# Below this a step's cost says nothing about the estimate.
NOTICEABLE_USD = 0.01
REPAIRED = {DiagnosisStatus.FIXED, DiagnosisStatus.PLATFORM_FIXED}
# Pauses that are the user's own doing or a restart, not a problem.
ORDINARY_PAUSES = {"interrupted", "user_paused"}
# Words of a failing command that are not the program it is about: ones that end the part of
# the command they start (cd somewhere, source a script, set options, print something), and ones
# that only come before the program (bash -lc, sudo, time).
REST = {"cd", "source", ".", "set", "export", "echo", "printf", "cat", "ls", "rm", "mkdir", "test", "[", "true"}
BEFORE = {"bash", "sh", "env", "sudo", "time", "nohup", "exec", "then", "if", "for", "do", "while", "{", "("}
LOOKUP = {"which", "command", "type"}


def program(command: str) -> Optional[str]:
    """The program a failing shell command was about: ``vcs`` for
    ``source .edarc && which vcs && vcs -ID``."""
    for part in re.split(r"&&|\|\||;|\||\n", command):
        words = [word.strip("'\"") for word in part.split() if "=" not in word]
        words = [word for word in words if word]
        while words and (words[0] in BEFORE or words[0].startswith("-")):
            words = words[1:]
        if not words or words[0] in REST:
            continue
        if words[0] in LOOKUP:
            words = [word for word in words[1:] if not word.startswith("-")]
            if not words:
                continue
        name = words[0].rsplit("/", 1)[-1]
        if re.fullmatch(r"[A-Za-z][\w.+-]*", name):
            return name
    return None


def _failing_program(run: ExecutionRun, node_id: Optional[str]) -> str:
    node = next((item for item in run.plan.nodes if item.id == node_id), None)
    names = Counter(
        name for attempt in (node.attempts if node else []) for call in attempt.tool_calls
        if not call.ok and call.tool in ("Bash", "run_command") and (name := program(call.detail))
    )
    return names.most_common(1)[0][0] if names else "general"


def _headline(report: Optional[str]) -> str:
    """The first line of a report that says something (not a heading or a bare label)."""
    for line in (report or "").splitlines():
        text = line.strip().lstrip("#*-> ").strip()
        if len(text) > 12:
            return text
    return ""


def _problem(kind: str, key: str, label: str, node_id: Optional[str] = None, repaired: bool = False,
             detail: str = "") -> dict:
    return {"kind": kind, "key": key, "label": label[:200], "node_id": node_id, "repaired": repaired,
            "detail": detail[:600]}


def repaired_areas(files: list[str]) -> list[tuple[str, str]]:
    """What a repair touched, as problem names: ("platform", module) for Super Partner's own
    source (agent for agent.py), ("environment", tool) for the shared tools folder (ssh for
    bin/ssh). Tests and documents are not areas."""
    areas = []
    for name in files:
        path = name.replace("\\", "/")
        stem = path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        if path.startswith(("backend/super_partner/", "frontend/")) and stem not in ("__init__", ""):
            areas.append(("platform", stem))
        elif path.startswith(("bin/", "python/", "node/")) or path == "sandbox.json":
            areas.append(("environment", stem))
    return list(dict.fromkeys(areas))


def review(run: ExecutionRun, events: list, repairs: list[dict] = ()) -> dict:
    """The look back on one task: its steps and the problems it ran into. ``events`` are the
    task's (db.events), ``repairs`` the repairs made for it: shared repair records and the
    platform engineer's applied jobs, as dicts with files and a report."""
    steps, problems = [], []
    for node in run.plan.nodes:
        spent = round(sum(attempt.cost_usd for attempt in node.attempts), 6)
        turns = max((attempt.turns for attempt in node.attempts), default=0)
        expected = loop_turns(node)
        steps.append({
            "id": node.id, "title": node.title, "model_id": node.model_id, "status": node.status,
            "estimated_usd": node.estimated_cost_usd, "actual_usd": spent, "expected_turns": expected,
            "turns": turns, "attempts": len(node.attempts),
        })
        if node.estimated_cost_usd and spent >= NOTICEABLE_USD and spent > OVER * node.estimated_cost_usd:
            problems.append(_problem("estimate_low", node.model_id or "?",
                                     f"花费 ${spent:.3f}，估算 ${node.estimated_cost_usd:.3f}", node.id))
        for attempt in node.attempts:
            if attempt.turns and attempt.turns > OVER * expected:
                problems.append(_problem("turns_over", attempt.model_id or "?",
                                         f"用了 {attempt.turns} 轮，预期 {expected} 轮", node.id))
            if attempt.outcome in (Outcome.UNAVAILABLE, Outcome.QUOTA):
                problems.append(_problem("model_unavailable", attempt.model_id or "?", attempt.reason or "", node.id))
    for repair in repairs:
        report = repair.get("report") or ""
        for kind, key in repaired_areas(repair.get("files") or []):
            problems.append(_problem(kind, key, _headline(report), repair.get("node_id"), repaired=True, detail=report))
    shared = {item.get("id") for item in repairs}
    for diagnosis in run.diagnoses:
        if diagnosis.status != "done" or diagnosis.verdict in (None, DiagnosisStatus.RETRY) or diagnosis.id in shared:
            continue  # nothing found, or its repair is counted above
        if diagnosis.verdict in (DiagnosisStatus.QUOTA_EXHAUSTED, DiagnosisStatus.MODEL_UNAVAILABLE):
            continue  # the failed attempt is already counted as model_unavailable
        if diagnosis.verdict.startswith("platform") or diagnosis.scope == "super_partner":
            continue  # a platform repair leaves a repair record, counted above
        problems.append(_problem(
            "environment", _failing_program(run, diagnosis.node_id),
            _headline(diagnosis.report) or diagnosis.verdict, diagnosis.node_id,
            repaired=diagnosis.verdict in REPAIRED, detail=diagnosis.report or "",
        ))
    for event in events:
        data = event.data or {}
        if event.kind == "approval.required" and data.get("reason") not in ORDINARY_PAUSES:
            problems.append(_problem("paused", str(data.get("reason")), "任务暂停，等用户处理", data.get("node_id")))
        elif event.kind in ("node.progress_check", "node.overrun_check") and data.get("stop"):
            problems.append(_problem("stuck", str(data.get("model_id")), str(data.get("reason") or ""), data.get("node_id")))
    estimated = round(sum(step["estimated_usd"] or 0 for step in steps), 6)
    return {
        "run_id": run.id, "status": run.status, "prompt": run.prompt[:200],
        "started_at": run.created_at.isoformat(), "finished_at": run.updated_at.isoformat(),
        "estimated_usd": estimated, "actual_usd": run.actual_cost_usd, "steps": steps, "problems": problems,
    }


def record(db, run_id: str) -> Optional[dict]:
    """Sum up a finished task and keep it (db.save_retrospective). Never raises: looking back
    must not break the task's end."""
    try:
        run = db.get_run(run_id)
        if not run or run.status not in FINISHED:
            return None
        repairs = [item for item in db.shared_repairs(limit=1000) if item.get("run_id") == run_id]
        repairs += [{"id": job.id, "files": job.files, "report": job.report or job.request, "node_id": job.node_id}
                    for job in db.engineer_jobs(limit=1000)
                    if job.run_id == run_id and job.status == "applied" and job.origin != "user"]
        body = review(run, db.events(run_id), repairs)
        db.save_retrospective(run_id, body)
        return body
    except Exception:
        logger.exception("Could not look back on task %s", run_id)
        return None


def all_reviews(db) -> list[dict]:
    """Every finished task's look back, oldest first; finished tasks without one get one now."""
    kept = {item["run_id"]: item for item in db.retrospectives()}
    for run in db.list_runs():
        if run.status in FINISHED and run.id not in kept and (body := record(db, run.id)):
            kept[run.id] = body
    return sorted(kept.values(), key=lambda item: item["started_at"])


def recurring(db, min_runs: int = 1) -> list[dict]:
    """Each kind of problem across tasks: in how many it came up, when, whether it was repaired,
    and whether it came back in a later task after a repair. Most frequent first."""
    groups: dict[tuple[str, str], dict] = {}
    for body in all_reviews(db):
        for problem in body["problems"]:
            group = groups.setdefault((problem["kind"], problem["key"]), {
                "kind": problem["kind"], "key": problem["key"], "runs": [], "repaired_in": [],
                "label": "", "detail": "", "recurred_after_repair": False,
            })
            if body["run_id"] not in group["runs"]:
                repaired_before = bool(group["repaired_in"])
                group["runs"].append(body["run_id"])
                group["recurred_after_repair"] |= repaired_before
            if problem["repaired"] and body["run_id"] not in group["repaired_in"]:
                group["repaired_in"].append(body["run_id"])
            group["label"] = problem["label"] or group["label"]
            group["detail"] = problem["detail"] or group["detail"]
            group.setdefault("last_seen", body["started_at"])
            group["last_seen"] = max(group["last_seen"], body["started_at"])
    found = [group for group in groups.values() if len(group["runs"]) >= min_runs]
    return sorted(found, key=lambda group: (-len(group["runs"]), group["kind"], group["key"]))


def signatures(run: ExecutionRun, node) -> set[tuple[str, str]]:
    """The names of the problem a failing step is in: its failing program, the model it ran on."""
    names = {("environment", _failing_program(run, node.id)), ("platform", _failing_program(run, node.id))}
    if node.model_id:
        names |= {("model_unavailable", node.model_id), ("turns_over", node.model_id), ("stuck", node.model_id)}
    return names


def similar(db, run: ExecutionRun, node, limit: int = 5) -> list[str]:
    """Lines for a diagnosis: the same kind of problem in other tasks, with how it went."""
    wanted = signatures(run, node)
    lines = []
    for group in recurring(db):
        others = [item for item in group["runs"] if item != run.id]
        if (group["kind"], group["key"]) not in wanted or not others or group["key"] == "general":
            continue
        repaired = f"repaired in {len(group['repaired_in'])} of them" if group["repaired_in"] else "never repaired"
        back = "; it came back after a repair, so the earlier repair did not remove the cause" if group["recurred_after_repair"] else ""
        lines.append(f"- {group['kind']}:{group['key']} in {len(others)} earlier task(s), last {group['last_seen'][:10]}, "
                     f"{repaired}{back}. Latest report: {group['detail'][:400] or group['label']}")
        if len(lines) >= limit:
            break
    return lines
