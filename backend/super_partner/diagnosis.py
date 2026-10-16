"""Diagnosing a stuck task: what went wrong, why, and what the next attempt should change.

An automatic diagnosis goes further: after every failed attempt of a step, the strong model
looks for what blocks it outside the task itself (missing software, sandbox or machine limits,
configuration, licenses) and repairs that. Its verdict decides what happens next: FIXED (run
the step again), RETRY (retry with its advice), QUOTA_EXHAUSTED (switch the execution model)
or BLOCKED (the user must act; the task pauses with the report).
"""
from __future__ import annotations

import json
import re
from typing import Optional

from .models import ExecutionRun, Outcome, TaskNode
from .recovery import SWITCH_AFTER, recent_failures

MAX_RESULT_CHARS = 2_000
# Tool calls of a node's latest attempt shown to a diagnosis.
MAX_LISTED_CALLS = 80
ADVICE = re.compile(r"^\s*NEXT_STEP:\s*(.+?)\s*(?=^\s*STATUS:|\Z)", re.IGNORECASE | re.MULTILINE | re.DOTALL)
# The option a diagnosis offers to let the task use the internet; choosing it turns that on.
NETWORK_CHOICE = "允许本任务联网"
STATUS = re.compile(r"^\s*STATUS:\s*(PLATFORM_REPAIR|PLATFORM_PENDING|NEEDS_INPUT|FIXED|RETRY|QUOTA_EXHAUSTED|MODEL_UNAVAILABLE|BLOCKED|UNABLE)\b", re.IGNORECASE | re.MULTILINE)
QUESTION = re.compile(r"^[ \t]*QUESTION:[ \t]*(\S[^\n]*)$", re.IGNORECASE | re.MULTILINE)
OPTIONS = re.compile(r"^[ \t]*OPTIONS:[ \t]*(\[[^\n]*\])[ \t]*$", re.IGNORECASE | re.MULTILINE)
INTERACTION_INSTRUCTIONS = (
    "\n# Talk with the user\n"
    "Consider the user's diagnosis replies below as their preferences and corrections. "
    "When a real choice, missing requirement, or preference prevents the next repair, ask one "
    "specific question in Chinese with `QUESTION: <question>` and optionally "
    '`OPTIONS: ["first concrete option", "second concrete option"]`, then end with '
    "`STATUS: NEEDS_INPUT`. The UI supports both a choice and a free-text answer, and the "
    "answer will automatically continue diagnosis. Do not infer a choice before the user "
    "answers. Do not ask permission for routine repairs already authorized by the task. "
    "For completed diagnoses you may also include a follow-up QUESTION and OPTIONS without "
    "blocking execution. Use at most 5 mutually distinct options; do not add an Other option."
)
USER_ACTION = re.compile(
    r"^[ \t]*USER_ACTION:[ \t]*(ROOT|CREDENTIALS|BUDGET|DECISION)[ \t]*:[ \t]*(\S[^\n]*)$",
    re.IGNORECASE | re.MULTILINE,
)
REPORT_FORMAT = (
    "Chinese Markdown with these sections: ## 问题定位 (which step fails and how), ## 根本原因 "
    "(the real cause, e.g. impossible acceptance criteria, missing tools or permissions, a wrong "
    "plan, a weak model, unclear instructions, environment limits), ## 处理结果与下一步 (what "
    "was checked, what was actually fixed, and what the system will do next; ask the user only "
    "for genuinely missing requirements, preferences, credentials, permissions or budget). Then one "
    "line starting with `NEXT_STEP:` followed by precise instructions, in Chinese, for the next "
    "attempt of the failing step, so that it succeeds."
)


def _tool_summary(attempt, last: bool) -> str:
    """How an API model spent its turns: calls per tool, and for the latest attempt the calls
    themselves, so a diagnosis can see where the turns went."""
    counts: dict[str, list[int]] = {}
    for call in attempt.tool_calls:
        tally = counts.setdefault(call.tool, [0, 0])
        tally[0] += 1
        tally[1] += not call.ok
    spread = ", ".join(f"{tool}×{n}" + (f" ({bad} failed)" if bad else "") for tool, (n, bad) in counts.items())
    head = f"  tool calls: {attempt.turns} turns{' (ran out of turns)' if attempt.turn_limit else ''}: {spread}"
    if not last:
        return head
    calls = [f"    t{call.turn} {call.tool} {call.detail}{'' if call.ok else '  [failed]'}" for call in attempt.tool_calls[-MAX_LISTED_CALLS:]]
    return "\n".join([head, *calls])


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
            if attempt.tool_calls:
                lines.append(_tool_summary(attempt, last=number == len(node.attempts)))
        if node.feedback:
            lines.append(f"Pending feedback: {node.feedback}")
        if node.changed_files:
            lines.append(f"Changed files: {', '.join(node.changed_files[:40])}")
        if node.result:
            clipped = node.result[:MAX_RESULT_CHARS]
            lines.append(f"Last result:\n{clipped}" + ("\n[...]" if len(node.result) > len(clipped) else ""))
    if events:
        lines.append("\nRecent events:\n" + "\n".join(events[-40:]))
    replied = sorted((d for d in run.diagnoses if d.messages), key=lambda d: d.messages[-1].created_at)
    for diagnosis in replied[-5:]:
        if diagnosis.messages:
            lines.append(f"\nPrevious diagnosis ({diagnosis.id}):\n{(diagnosis.report or '')[:2000]}")
            if diagnosis.question:
                lines.append(f"Question to the user: {diagnosis.question}")
            for message in diagnosis.messages[-5:]:
                lines.append(f"User's reply: choice={message.choice or '(free text)'}\n{message.text}")
    lines.append(INTERACTION_INSTRUCTIONS)
    return lines


def platform_instructions(context: str) -> str:
    return (
        "\n# Super Partner itself\n"
        "Distinguish a task mistake, a shared environment problem, and a defect in Super Partner "
        "itself (scheduler, model fallback, budgets, tools, persistence, API, or web UI). "
        "The user authorizes fixing Super Partner defects, including common causes affecting "
        "other projects. Do not hand an actionable platform bug back to the user. "
        "Read the application source and correlate it with the failing operation and evidence. "
        "If the application needs a code fix, describe the specific defect and a regression check, "
        "give one `LOCATION: path:line function — what is wrong` line per place to change (the "
        "repair phase starts from them), then end with `STATUS: PLATFORM_REPAIR`. This starts a separate "
        "source-editing and validation phase automatically. Do not edit the live application "
        "or restart services in this investigation phase. An upstream quota/outage alone is "
        "not a platform bug: use the existing model-switch statuses.\n" + context
    )


def prompt(run: ExecutionRun, node_id: Optional[str], workspace: str, events: list[str],
           platform: str = "") -> str:
    """Analysis only: why the task is stuck, for the 诊断 button."""
    lines = [
        ("You are diagnosing a multi-model task that is not completing. Investigate, but do not "
         "modify any files: only read them and run read-only checks."),
        f"Working directory of the task: {workspace}",
        "",
        *_history(run, events),
        platform_instructions(platform),
    ]
    focus = next((node for node in run.plan.nodes if node.id == node_id), None)
    if focus:
        lines.append(f"\nFocus on step '{focus.title}', which ran out of attempts.")
    lines.append(f"\nWrite the diagnosis in {REPORT_FORMAT}")
    lines.append("If the user's replies resolve a pending question and work can continue, end with STATUS: RETRY.")
    return "\n".join(lines)


def repair_prompt(run: ExecutionRun, node: TaskNode, workspace: str, tools: str, ports: list[int], events: list[str],
                  shared_repairs: tuple | list = (), platform: str = "") -> str:
    """Find and repair what blocks a step from outside the task, or say what should happen."""
    port_note = f"local ports {', '.join(map(str, ports))} only" if ports else "nothing"
    last = node.attempts[-1] if node.attempts else None
    return "\n".join([
        (f"Attempt {len(node.attempts)} of step '{node.title}' in a multi-model task just failed"
         + (f" ({last.outcome}: {(last.reason or '')[:300]})" if last else "") + ". Find out why, "
         "and repair whatever blocks it outside the task itself: missing software, sandbox or "
         "machine limits, configuration, licenses, unreachable services. Do not do the step's work "
         "and do not change the task's files; the scheduler selects the model for the next attempt."),
        *_repeated(node),
        *([("The attempt reported that part of the step cannot be done in this environment (outcome "
            "`unable`). Lift that limit if you can (FIXED), or ask the user when only they can decide "
            "(NEEDS_INPUT). If the claim is wrong and the step can be done as is, say RETRY and how. "
            "Otherwise answer BLOCKED with the USER_ACTION that would make it possible: the step is "
            "then reported as not doable here, with your report, instead of pausing the task.")]
          if last and last.outcome == Outcome.UNABLE else []),
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
         "cleared, " + (
             f"and this task allows the internet, but not this machine's local services (reachable: {port_note}). "
             "Membership CLIs' sandboxes also allow the internet for this task."
             if run.allow_network else
             f"and the network is cut off (reachable: {port_note}). Membership CLIs run in their own "
             "sandboxes, also without network."
         ) + " So software installed under the home folder or only added to a shell profile is invisible to them."),
        *([] if run.allow_network else [
            ("This task does not allow the internet. Install what a step needs yourself (you have "
             "network access). Only if the step itself must reach the internet while it runs (call an "
             "external API, download data it processes), ask with a QUESTION line and "
             f'OPTIONS that include exactly "{NETWORK_CHOICE}", then `STATUS: NEEDS_INPUT`; choosing it '
             "allows the internet for this task's commands from the next attempt on."),
        ]),
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
         "Project-specific changes must not become global settings. Recent shared repair "
         "records follow; only fixed/activated records succeeded. Verify applicability before reuse:"),
        *[f"- {item['id']} scope={item.get('scope', 'environment')} status={item.get('status', 'fixed')}: "
          f"files={item.get('files', [])}\n{item.get('report', '')[:2000]}\n{item.get('error') or ''}"
          for item in shared_repairs],
        "",
        *_history(run, events),
        platform_instructions(platform),
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
         "settings or change scheduler code merely to bypass the upstream limit. A separate, "
         "evidenced scheduler defect uses STATUS: PLATFORM_REPAIR instead.\n"
         "- `STATUS: MODEL_UNAVAILABLE` if the failed step's model service is unavailable "
         "(HTTP 5xx, overload, timeout, or a broken provider connection). Quote the exact error. "
         "The scheduler will temporarily exclude it for all projects and switch to a backup; "
         "do not tell the user to wait or select another already configured model manually.\n"
         "- `STATUS: UNABLE` if the step's goal cannot be reached in this environment and you cannot "
         "change that (software that cannot be installed without root, hardware, a licence, an account "
         "or credentials the task does not have): say what is missing and what would make it possible, "
         "with a `USER_ACTION:` line as for BLOCKED. The step is reported as not doable here and the "
         "rest of the task goes on, instead of retrying it.\n"
         "- `STATUS: BLOCKED` if the user must act first (it needs root, a license or account, a "
         "decision only the user can make, or the plan or acceptance criteria are impossible): write "
         f"a report in {REPORT_FORMAT} Then include a `USER_ACTION: <ROOT|CREDENTIALS|BUDGET|DECISION>: "
         "<exact missing permission, credential, budget increase, or decision>` line before STATUS. "
         "State the evidence and which authorized alternatives you tried. Root is only a blocker "
         "when a user-local shared installation or configuration cannot solve it. Without a "
         "specific required user action, the scheduler treats BLOCKED as a retry."),
    ])


def _repeated(node: TaskNode) -> list[str]:
    """A warning when the step keeps failing, so that a diagnosis does not answer RETRY again
    for a limit that another attempt will meet as well."""
    failures = recent_failures(node)
    if len(failures) < SWITCH_AFTER:
        return []
    reasons = "\n".join(f"- {item.model_id}: {(item.reason or '')[:200]}" for item in failures[-4:])
    return [(f"This step has now failed {len(failures)} times in a row:\n{reasons}\n"
             "If the same cause keeps coming back, another RETRY with advice will not help. Find what "
             "keeps failing. If it is outside the task (a missing tool, simulator, licence, network, "
             "credentials), fix it (FIXED), ask the user (NEEDS_INPUT), or answer UNABLE. Answer RETRY "
             "only if the next attempt will do something materially different, and say what.")]


def parse(text: str) -> tuple[str, Optional[str], Optional[str]]:
    """The report, NEXT_STEP advice, and scheduler status if given."""
    text = text or ""
    verdict = STATUS.search(text)
    status = verdict.group(1).lower() if verdict else None
    body = text[:verdict.start()] if verdict else text
    body = USER_ACTION.sub("", body)
    body = QUESTION.sub("", OPTIONS.sub("", body))
    match = ADVICE.search(body)
    if not match:
        return body.strip(), None, status
    return body[:match.start()].strip(), match.group(1).strip(), status


def user_action(text: str) -> Optional[str]:
    match = USER_ACTION.search(text or "")
    return f"{match.group(1).upper()}: {match.group(2).strip()}" if match else None


def interaction(text: str) -> tuple[Optional[str], list[str]]:
    question = QUESTION.search(text or "")
    options = OPTIONS.search(text or "")
    choices = []
    if question and options:
        try:
            raw = json.loads(options.group(1))
            if isinstance(raw, list):
                choices = list(dict.fromkeys(item.strip()[:300] for item in raw
                                            if isinstance(item, str) and item.strip()))[:5]
        except (ValueError, TypeError):
            pass
    return (question.group(1).strip()[:1000] if question else None), choices
