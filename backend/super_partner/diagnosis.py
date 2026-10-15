"""Diagnosing a stuck task: what went wrong, why, and what the next attempt should change.

An automatic diagnosis goes further: after every failed attempt of a step, the strong model
looks for what blocks it outside the task itself (missing software, sandbox or machine limits,
configuration, licenses) and repairs that. Its verdict decides what happens next: FIXED (run
the step again), RETRY (retry with its advice), QUOTA_EXHAUSTED (switch the execution model)
or BLOCKED (the user must act; the task pauses with the report).
"""
from __future__ import annotations

import re
from typing import Optional

from .models import ExecutionRun, TaskNode

MAX_RESULT_CHARS = 2_000
ADVICE = re.compile(r"^\s*NEXT_STEP:\s*(.+?)\s*(?=^\s*STATUS:|\Z)", re.IGNORECASE | re.MULTILINE | re.DOTALL)
STATUS = re.compile(r"^\s*STATUS:\s*(FIXED|RETRY|QUOTA_EXHAUSTED|MODEL_UNAVAILABLE|BLOCKED)\b", re.IGNORECASE | re.MULTILINE)
USER_ACTION = re.compile(
    r"^[ \t]*USER_ACTION:[ \t]*(ROOT|CREDENTIALS|BUDGET|DECISION)[ \t]*:[ \t]*(\S[^\n]*)$",
    re.IGNORECASE | re.MULTILINE,
)
REPORT_FORMAT = (
    "Chinese Markdown with these sections: ## 问题定位 (which step fails and how), ## 根本原因 "
    "(the real cause, e.g. impossible acceptance criteria, missing tools or permissions, a wrong "
    "plan, a weak model, unclear instructions, environment limits), ## 建议操作 (concrete actions "
    "for the user, such as changing the budget, enabling a model, or editing the plan). Then one "
    "line starting with `NEXT_STEP:` followed by precise instructions, in Chinese, for the next "
    "attempt of the failing step, so that it succeeds."
)


def _history(run: ExecutionRun, events: list[str]) -> list[str]:
    """The request, the plan, and every attempt of every step."""
    lines = [
        f"User request:\n{run.prompt}",
        (f"\nTask status: {run.status.value}; spent ${run.actual_cost_usd:.4f} of ${run.budget_usd:.2f}; "
         f"rework rounds {run.rework_rounds}."),
    ]
    if run.error:
        lines.append(f"Task error: {run.error}")
    lines.append("\nPlan steps:")
    for index, node in enumerate(run.plan.nodes, 1):
        lines.append(
            f"\n## {index:02d} {node.title} [{node.status.value}] model={node.model_id} "
            f"difficulty={node.difficulty} capability={node.capability}"
        )
        lines.append(f"Instructions: {node.instructions}")
        if node.acceptance_criteria:
            lines.append(f"Acceptance criteria: {node.acceptance_criteria}")
        if node.dependencies:
            lines.append(f"Depends on: {', '.join(node.dependencies)}")
        for number, attempt in enumerate(node.attempts, 1):
            lines.append(
                f"- attempt {number}: {attempt.outcome} by {attempt.model_id}"
                + (f" (reviewed by {attempt.reviewer_model_id})" if attempt.reviewer_model_id else "")
                + f": {attempt.reason or ''}"
            )
        if node.feedback:
            lines.append(f"Pending feedback: {node.feedback}")
        if node.changed_files:
            lines.append(f"Changed files: {', '.join(node.changed_files[:40])}")
        if node.result:
            clipped = node.result[:MAX_RESULT_CHARS]
            lines.append(f"Last result:\n{clipped}" + ("\n[...]" if len(node.result) > len(clipped) else ""))
    if events:
        lines.append("\nRecent events:\n" + "\n".join(events[-40:]))
    return lines


def prompt(run: ExecutionRun, node_id: Optional[str], workspace: str, events: list[str]) -> str:
    """Analysis only: why the task is stuck, for the 诊断 button."""
    lines = [
        ("You are diagnosing a multi-model task that is not completing. Investigate, but do not "
         "modify any files: only read them and run read-only checks."),
        f"Working directory of the task: {workspace}",
        "",
        *_history(run, events),
    ]
    focus = next((node for node in run.plan.nodes if node.id == node_id), None)
    if focus:
        lines.append(f"\nFocus on step '{focus.title}', which ran out of attempts.")
    lines.append(f"\nWrite the diagnosis in {REPORT_FORMAT}")
    return "\n".join(lines)


def repair_prompt(run: ExecutionRun, node: TaskNode, workspace: str, tools: str, ports: list[int], events: list[str],
                  shared_repairs: tuple | list = ()) -> str:
    """Find and repair what blocks a step from outside the task, or say what should happen."""
    port_note = f"local ports {', '.join(map(str, ports))} only" if ports else "nothing"
    last = node.attempts[-1] if node.attempts else None
    return "\n".join([
        (f"Attempt {len(node.attempts)} of step '{node.title}' in a multi-model task just failed"
         + (f" ({last.outcome}: {(last.reason or '')[:300]})" if last else "") + ". Find out why, "
         "and repair whatever blocks it outside the task itself: missing software, sandbox or "
         "machine limits, configuration, licenses, unreachable services. Do not do the step's work "
         "and do not change the task's files; the scheduler selects the model for the next attempt."),
        f"Working directory of the task: {workspace}",
        "",
        "# What you may do",
        ("You are responsible for completing recoverable repairs, not merely suggesting them. "
         "Use your tools to make and verify the fix before reporting FIXED. Install missing tools "
         "in the shared user-writable tools folder when possible instead of asking for root. "
         "For an upstream model outage or exhausted usage, return the model-switch status below; "
         "the scheduler can solve that without the user. Do not claim a repair from a proposal alone."),
        ("Your commands run on the host as the user, with network access, but without root: sudo "
         "and other setuid programs are blocked. Anything that needs root (system packages, "
         "system services, files under /etc) is for the user: put the exact commands in the report."),
        "",
        "# Where the step's commands run",
        ("API models' commands run in a bubblewrap sandbox: the system is read-only, the home folder "
         "is hidden (only the working directory is mounted), /tmp is private, the environment is "
         f"cleared, and the network is cut off (reachable: {port_note}). Membership CLIs run in "
         "their own sandboxes, also without network. So software installed under the home folder "
         "or only added to a shell profile is invisible to them."),
        f"Make repairs visible to every step through the shared tools folder {tools}:",
        f"- executables, or symlinks to them, go in {tools}/bin, which is first on PATH;",
        f"- Python packages: `pip install --target {tools}/python ...` ({tools}/python is on PYTHONPATH);",
        f"- npm packages: `npm install --prefix {tools}/node ...`, then symlink their binaries into {tools}/bin;",
        (f"- environment variables, and local TCP ports (such as a license server) that sandboxed "
         f"commands must reach, go in {tools}/sandbox.json: "
         '`{"env": {"NAME": "value"}, "host_ports": [26585]}`; keep what is already there.'),
        ("The folder is mounted read-only into the sandbox at the same path, so symlinks to elsewhere "
         "under the home folder do not work there; copy or install into the folder instead."),
        ("Then check the repair the way the step will use it, e.g. run the tool by its name with PATH "
         f"set to {tools}/bin:/usr/bin:/bin."),
        ("Fix common environment causes in this shared folder, preserving existing configuration, "
         "so all projects benefit. Verify from the failing step's sandbox as well as the host. "
         "Include the changed shared paths, check commands and actual results in the report. "
         "Project-specific changes must not become global settings. Previously applied shared "
         "repairs follow; verify they still apply before reusing them:"),
        *[f"- {item['id']}: files={item.get('files', [])}\n{item.get('report', '')[:2000]}"
          for item in shared_repairs],
        "",
        *_history(run, events),
        ("\n# How to answer\n"
         "Answer in Chinese Markdown and end with exactly one status line:\n"
         "- `STATUS: FIXED` if you repaired the environment so the step can now succeed: describe "
         "under `## 修复内容` what was wrong and exactly what you changed and checked.\n"
         "- `STATUS: RETRY` if nothing outside the task blocks it (the model made a mistake, ran out "
         "of turns, or misread the instructions) and another attempt can succeed: say briefly why it "
         "failed, then a `NEXT_STEP:` line telling the next attempt precisely what to do differently.\n"
         "- `STATUS: QUOTA_EXHAUSTED` if the failed step's execution model has exhausted its "
         "subscription/session/weekly usage or API credits, or is rate-limited. Quote its exact "
         "error and reset time (including timezone) in the report. The scheduler will mark that "
         "execution model unavailable and hand the original step to another eligible model, "
         "preserving completed work and feedback. This status concerns the step's latest failure, "
         "not an older failure or a diagnostic model. Do not return BLOCKED or merely recommend "
         "waiting, buying credits, or manually changing models for this case. Do not change account "
         "settings or scheduler source code.\n"
         "- `STATUS: MODEL_UNAVAILABLE` if the failed step's model service is unavailable "
         "(HTTP 5xx, overload, timeout, or a broken provider connection). Quote the exact error. "
         "The scheduler will temporarily exclude it for all projects and switch to a backup; "
         "do not tell the user to wait or select another already configured model manually.\n"
         "- `STATUS: BLOCKED` if the user must act first (it needs root, a license or account, a "
         "decision only the user can make, or the plan or acceptance criteria are impossible): write "
         f"a report in {REPORT_FORMAT} Then include a `USER_ACTION: <ROOT|CREDENTIALS|BUDGET|DECISION>: "
         "<exact missing permission, credential, budget increase, or decision>` line before STATUS. "
         "State the evidence and which authorized alternatives you tried. Root is only a blocker "
         "when a user-local shared installation or configuration cannot solve it. Without a "
         "specific required user action, the scheduler treats BLOCKED as a retry."),
    ])


def parse(text: str) -> tuple[str, Optional[str], Optional[str]]:
    """The report, NEXT_STEP advice, and scheduler status if given."""
    text = text or ""
    verdict = STATUS.search(text)
    status = verdict.group(1).lower() if verdict else None
    body = text[:verdict.start()] if verdict else text
    body = USER_ACTION.sub("", body)
    match = ADVICE.search(body)
    if not match:
        return body.strip(), None, status
    return body[:match.start()].strip(), match.group(1).strip(), status


def user_action(text: str) -> Optional[str]:
    match = USER_ACTION.search(text or "")
    return f"{match.group(1).upper()}: {match.group(2).strip()}" if match else None
