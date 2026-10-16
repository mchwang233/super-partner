"""The task's lead: the strong model that plans a task keeps one conversation with it until the
task ends, and is asked what to do as things happen (docs/design/supervisor.md).

Here: choosing the lead, starting and continuing its conversation, and handing the task over to
a new conversation when the old one is lost or its model runs out of quota."""

from __future__ import annotations

import asyncio
import json
from typing import Optional
from uuid import uuid4

from .budget import ProviderError
from .cli_channels import SESSION_CLIS, SessionLost
from .config import settings
from .models import ExecutionRun, ModelProfile, NodeStatus, SupervisorSession
from .quota import ModelUnavailable

ROLE = (
    "You are the lead of a multi-model task in Super Partner. You plan it, and you stay with it "
    "until it ends: as its steps run on other models, you will be told what happened and asked "
    "what to do next. Keep the whole task in mind: its goal, the plan and why each step exists, "
    "and what has happened so far. You may read files and run read-only commands to look things "
    "up; do not edit anything, the steps' models do the work. Answer in the format each message "
    "asks for.\n\n"
)
# An API lead's conversation is sent whole every time; past this, older exchanges are dropped.
MAX_HISTORY_CHARS = 300_000
# Leads tried in turn for one message when a conversation is lost or a model is unavailable.
MAX_LEADS = 3
# Step results quoted in a hand-over.
MAX_RESULT_CHARS = 600


def candidates(models: list[ModelProfile], configured: set[str], exclude=()) -> list[ModelProfile]:
    """Possible leads, best first: membership CLIs that keep conversations (no extra cost, and they
    can look at the files), then API models; the best planners first."""
    eligible = [
        model for model in models
        if model.enabled and model.routing_ready and model.id in configured and model.id not in exclude
        and model.id not in settings.helper_only_models
        and model.provider != "mock" and (model.channel != "cli" or model.id in SESSION_CLIS)
    ]
    return sorted(eligible, key=lambda model: (
        model.channel != "cli", -model.scores.planning, -model.scores.reliability,
        (model.input_per_million or 0) + (model.output_per_million or 0),
    ))


def new_session(model: ModelProfile, previous: Optional[SupervisorSession] = None) -> SupervisorSession:
    """A fresh conversation for ``model``, in a folder of its own (a CLI conversation is tied to it)."""
    session = SupervisorSession(model_id=model.id, cwd=str(settings.data_dir / "supervisors" / uuid4().hex))
    if previous and previous.model_id:
        session.previous = [*previous.previous, previous.model_id]
    return session


def trim(session: SupervisorSession) -> None:
    """Keep an API lead's conversation within MAX_HISTORY_CHARS: the first exchange (its role and
    the plan) stays, the oldest after it go."""
    def size():
        return sum(len(item.get("content") or "") for item in session.history)

    dropped = 0
    while size() > MAX_HISTORY_CHARS and len(session.history) > 4:
        del session.history[2:4]
        dropped += 1
    if dropped and not session.history[2]["content"].startswith("[Earlier events"):
        session.history[2]["content"] = (f"[Earlier events of this task were left out to save space; "
                                         f"ask for details if you need them.]\n\n{session.history[2]['content']}")


def handover(run: ExecutionRun) -> str:
    """What a new lead needs to take over a running task: the request, the plan and where every
    step stands, and the latest problems."""
    lines = [
        "You are taking over as the lead of a task that is already under way.",
        f"Request:\n{run.prompt}",
        *([f"Background:\n{run.background}"] if run.background else []),
        f"Plan: {run.plan.summary}",
        f"Working directory: {run.project_dir or settings.workspace_root / run.folder_id}",
        "Steps:",
    ]
    for node in run.plan.nodes:
        deps = f" (after {', '.join(node.dependencies)})" if node.dependencies else ""
        lines.append(f"- [{node.id}] {node.title}{deps}: {node.status.value}, {len(node.attempts)} attempts, "
                     f"model {node.model_id}")
        if node.status == NodeStatus.SUCCEEDED and (node.summary or node.result):
            lines.append(f"  result: {(node.summary or node.result)[:MAX_RESULT_CHARS]}")
        elif node.attempts and node.attempts[-1].outcome != "passed":
            lines.append(f"  last attempt: {node.attempts[-1].outcome}: {(node.attempts[-1].reason or '')[:MAX_RESULT_CHARS]}")
        if node.unable_reason:
            lines.append(f"  cannot be done here: {node.unable_reason[:MAX_RESULT_CHARS]}")
    if run.error:
        lines.append(f"Current problem: {run.error}")
    return "\n".join(lines) + "\n\n"


async def converse(providers, session: SupervisorSession, prompt: str, charged) -> tuple[str, SupervisorSession]:
    """Send ``prompt`` to ``session``'s lead through ``charged(model_id, call)``, which bills the
    call to the task. Returns the reply and the session, now continued."""
    result = await charged(session.model_id, lambda budget: providers.converse(session, prompt, spend_ok=budget))
    if session.history:
        trim(session)
    return result.text, session


async def ask(providers, models: list[ModelProfile], configured: set[str], run: ExecutionRun,
              prompt: str, charged) -> tuple[str, SupervisorSession]:
    """Ask the task's lead; a lost conversation or an unavailable model hands the task over to
    the next best lead, which is briefed first."""
    session = run.supervisor if run.supervisor and run.supervisor.model_id else None
    message = prompt
    tried: set[str] = set()
    by_id = {model.id: model for model in models}
    last_error: Optional[Exception] = None
    for _ in range(MAX_LEADS):
        if session is None:
            pick = next(iter(candidates(models, configured, tried)), None)
            if pick is None:
                break
            session = new_session(pick, run.supervisor)
            message = ROLE + handover(run) + prompt
        try:
            return await converse(providers, session, message, charged)
        except SessionLost as exc:
            # The model is fine; only its conversation is gone. Brief a new one.
            last_error = exc
            model = by_id.get(session.model_id)
            session = new_session(model, session) if model else None
            message = ROLE + handover(run) + prompt
        except (ModelUnavailable, ProviderError, asyncio.TimeoutError) as exc:
            last_error = exc
            tried.add(session.model_id)
            session = None
    raise ProviderError(f"没有可用的负责人模型：{last_error or '没有可用的模型'}")


# --- deciding what to do after a failed attempt -------------------------------------------------

ACTIONS = ("retry", "switch_model", "accept", "unable", "ask_user", "repair_environment", "repair_platform",
           "rework", "replan", "done")
# What the lead may answer when every step has finished.
FINAL_ACTIONS = ("done", "rework", "replan")
ACTION_RULES = {
    "rework": "- rework: send the verified steps listed in targets back to be redone, with instructions "
              "saying exactly what to change. Only for real problems the task needs fixed; not for new "
              "requirements the task never asked for, and not when the verification itself is wrong.\n",
    "retry": "- retry: the same model again. Only if the next attempt will do something materially different.\n",
    "switch_model": "- switch_model: another model from the list takes the step (a model that keeps failing "
                    "it, or one too weak for it).\n",
    "accept": "- accept: the result is good enough for the task's goal although its check failed (the check "
              "was wrong, or asked for more than the task needs); say why.\n",
    "unable": "- unable: the step's goal cannot be reached in this environment and no repair can change that "
              "(hardware, an account, a licence or credentials the task does not have). The rest of the "
              "task goes on without it.\n",
    "ask_user": "- ask_user: only the user can decide or provide something (a choice, credentials, budget, "
                "permission). The task pauses until they answer.\n",
    "repair_environment": "- repair_environment: something outside the task blocks it (missing software, sandbox "
                          "or machine limits, configuration). A repair session with access to this machine "
                          "fixes it, then the step runs again.\n",
    "repair_platform": "- repair_platform: Super Partner itself (its scheduler, tools or sandbox) is at fault; say "
                       "what and where. A source repair is made, checked and applied, then the task continues.\n",
    "replan": "- replan: change the plan in \"plan\": add steps, remove steps that have not finished, or rewrite "
              "the instructions of steps that have not finished (splitting a step that is too big, adding a "
              "missing prerequisite, dropping one the task does not need). Finished steps stay as they are.\n",
    "done": "- done: the result does what the request asked; the task ends.\n",
}
PLAN_FORMAT = (
    '"plan": {"add": [{"key": "new1", "title": "...", "instructions": "...", "acceptance_criteria": "...", '
    '"capability": "coding | planning | research | chinese | tool_use | structured_output", '
    '"difficulty": "easy | normal | hard", "dependencies": ["ids of existing steps or keys of added ones"], '
    '"verifies": ["optional: ids or keys of steps this one verifies"], '
    '"files": ["files it creates or changes, at most 5"], "reads": ["files it reads for reference"], '
    '"provides": "what later steps can rely on", "check": "a command that shows it works"}], '
    '"remove": ["ids of steps that have not finished"], '
    '"update": [{"id": "...", "title": "optional", "instructions": "optional", "acceptance_criteria": "optional", '
    '"dependencies": ["optional: the full new list"], "files": ["optional: the full new list"], '
    '"reads": ["optional"], "provides": "optional", "check": "optional"}]}'
)


def decision_format(rework: bool = False, final: bool = False) -> str:
    """How to answer; a failed verification may also send steps back (``rework``), and when
    every step has finished (``final``) the lead says whether the task is done."""
    actions = list(FINAL_ACTIONS) if final else [
        action for action in ACTIONS if action != "done" and (rework or action != "rework")]
    return (
        "Reply with only a JSON object:\n"
        f'{{"action": "{" | ".join(actions)}", '
        '"reason": "one or two sentences in Chinese, shown to the user", '
        '"instructions": "for retry / switch_model / rework: exactly what must be done differently; '
        'for repair_*: what to repair", "model_id": "switch_model only: one of the models listed", '
        + ('"targets": ["rework only: ids of the steps to redo"], ' if rework or final else "")
        + (PLAN_FORMAT + ", " if "replan" in actions else "")
        + '"question": "ask_user only, in Chinese", "options": ["ask_user only: up to 4 answers"], '
        '"user_action": "unable only: what the user could do to make it possible"}\n'
        + "".join(ACTION_RULES[action] for action in actions)
    )


DECISION_FORMAT = decision_format()
MAX_EVIDENCE_CHARS = 1500


def _steps(run: ExecutionRun) -> list[str]:
    return [f"- [{node.id}] {node.title}: {node.status.value}" for node in run.plan.nodes]


def _models(choices: list[ModelProfile], node) -> list[str]:
    return [f"- {model.id} ({model.channel}, {node.capability} {model.scores.score_for(node.capability):g}, "
            f"reliability {model.scores.reliability:g})" for model in choices]


def failure_message(run: ExecutionRun, node, attempt, verdict, choices: list[ModelProfile],
                    left: tuple[int, int]) -> str:
    """Tell the lead an attempt of ``node`` failed, with the evidence, and ask what to do."""
    from .diagnosis import NETWORK_CHOICE, _tool_summary

    earlier = [f"- {item.model_id}: {item.outcome}: {(item.reason or '')[:200]}" for item in node.attempts[:-1][-6:]]
    lines = [
        f"EVENT: an attempt failed. Step [{node.id}] {node.title} ({node.capability}, {node.difficulty}).",
        f"Attempt {len(node.attempts)} by {attempt.model_id}: {attempt.outcome}.",
        f"Why it was not accepted: {verdict.reason}",
        *([f"What the check says to change: {verdict.fix}"] if verdict.fix and verdict.fix != verdict.reason else []),
        *([f"Its answer (start):\n{node.result[:MAX_EVIDENCE_CHARS]}"] if node.result else []),
        *([_tool_summary(attempt, True)] if attempt.tool_calls else []),
        f"Files this step has changed: {', '.join(node.changed_files[:30]) or '(none)'}",
        *(["Earlier attempts of this step:", *earlier] if earlier else []),
        "Where the steps stand:", *_steps(run),
        "Models that can take this step:", *_models(choices, node),
        f"Decisions left: {left[0]} for this step, {left[1]} for the task; after that the task pauses for the user.",
        ("This task allows the internet." if run.allow_network else
         "This task does not allow the internet. If the step itself must reach it while it runs, "
         f'ask_user with "{NETWORK_CHOICE}" among the options; choosing it allows the internet at once.'),
        "",
        ("Decide what to do with this step. You may look at the files in the working directory "
         f"({run.project_dir or settings.workspace_root / run.folder_id}) first."),
        DECISION_FORMAT,
    ]
    return "\n".join(lines)


def verification_message(run: ExecutionRun, verifier, verdict, choices: list[ModelProfile],
                         left: tuple[int, int]) -> str:
    """Tell the lead a verification failed, with what it found and what it checked, and ask
    whether to send work back."""
    verified = [node for node in run.plan.nodes if node.id in verifier.verifies]
    lines = [
        f"EVENT: a verification failed. Step [{verifier.id}] {verifier.title} checks "
        + ", ".join(f"[{node.id}] {node.title}" for node in verified) + ".",
        f"What it found: {verdict.reason}",
        *([f"What it says to change: {verdict.fix}"] if verdict.fix and verdict.fix != verdict.reason else []),
        f"Its report (start):\n{(verifier.result or '')[:MAX_EVIDENCE_CHARS]}",
        f"Work sent back so far in this task: {run.rework_rounds} times.",
        "The steps it checks:",
    ]
    for node in verified:
        lines.append(f"- [{node.id}] {node.title}, by {node.model_id}, {len(node.attempts)} attempts; "
                     f"review: {(node.review or '')[:300]}")
        lines.append(f"  result: {(node.summary or node.result or '')[:800]}")
    lines += [
        "Where the steps stand:", *_steps(run),
        "Models that can run the verification:", *_models(choices, verifier),
        f"Decisions left: {left[0]} for this step, {left[1]} for the task; after that the task pauses for the user.",
        "",
        ("Judge the findings first: are they real problems the task needs fixed, new requirements it "
         "never asked for, or mistakes of the verification itself? Then decide."),
        decision_format(rework=True),
    ]
    return "\n".join(lines)


def final_message(run: ExecutionRun, left: int) -> str:
    """Every step has finished: the lead checks the result as a whole against the request."""
    lines = [
        ("EVENT: every step has finished. Before the task ends, check the result as a whole against "
         "the request: does it really do what was asked, and do the pieces fit together?"),
        f"Request:\n{run.prompt}",
        f"Working directory: {run.project_dir or settings.workspace_root / run.folder_id}",
        "The steps and their results:",
    ]
    for node in run.plan.nodes:
        lines.append(f"- [{node.id}] {node.title} (by {node.model_id}): {(node.review or '')[:200]}")
        lines.append(f"  result: {(node.summary or node.result or '')[:800]}")
    lines += [
        f"Decisions left for the task: {left}.",
        "",
        ("You may look at the files first. If something the request asked for is missing or wrong, "
         "rework the steps that should fix it, or replan to add what is missing."),
        decision_format(final=True),
    ]
    return "\n".join(lines)


def overrun_message(node, turns: int, expected: int, evidence: str) -> str:
    """A work package taking far more turns than planned: going on, or stopped (and then split)."""
    return "\n".join([
        (f"EVENT: work package [{node.id}] {node.title} was planned for about {expected} tool turns and has "
         f"taken {turns}."),
        f"Its files: {', '.join(node.files) or '-'}; reads: {', '.join(node.reads) or '-'}",
        f"What it has been doing:\n{evidence}",
        ("Is it still doing its own package and close to done, or is the package too big, under-specified, "
         "or going wrong? Stopped, the attempt fails and you then decide what to do (for a package too "
         "big: replan to split it). End with exactly one line: `VERDICT: CONTINUE` or "
         "`VERDICT: STOP: <why, in Chinese>`."),
    ])


def chat_message(text: str) -> str:
    """The user writing to the lead directly, not answering one of its questions."""
    return (
        "MESSAGE from the user (they can see the task, its steps and your decisions):\n"
        f"{text}\n\n"
        "Answer in Chinese, in a few sentences, as yourself: the lead who has followed this task. "
        "No decision JSON is needed here. If they ask for a change (to a step, the plan, or how you "
        "decide), say what you will do; you will act on it at your next decisions."
    )


MAX_REPAIR_REPORT = 3000


def repair_message(run: ExecutionRun, node, diagnosis, choices: list[ModelProfile], left: tuple[int, int]) -> str:
    """The repair the lead asked for has ended: hand it its outcome before the step runs again."""
    outcome = (f"It ended with status {diagnosis.verdict or 'none'}." if diagnosis.status == "done"
               else f"It did not finish: {diagnosis.error or 'no reason given'}.")
    return "\n".join([
        f"EVENT: the repair you asked for step [{node.id}] {node.title} has ended. {outcome}",
        f"Its report:\n{(diagnosis.report or '(none)')[:MAX_REPAIR_REPORT]}",
        "Where the steps stand:", *_steps(run),
        "Models that can take this step:", *_models(choices, node),
        f"Decisions left: {left[0]} for this step, {left[1]} for the task; after that the task pauses for the user.",
        "",
        ("Decide what to do with the step now. Run it again only if the report shows the obstacle was "
         "removed and checked (for example a login or command that now works); if the repair did not "
         "do that, do not send the step back to meet the same obstacle: repair again differently, ask "
         "the user, or mark it unable."),
        DECISION_FORMAT,
    ])


def answer_message(node, question: str, answer: str, rework: bool = False) -> str:
    """Hand the user's answer to the lead's question back to it."""
    return "\n".join([
        f"EVENT: the user answered your question about step [{node.id}] {node.title}.",
        f"Your question: {question}",
        f"Their answer: {answer}",
        "",
        "Decide what to do with this step now.",
        decision_format(rework),
    ])


def parse_decision(text: str, model_ids: set[str], targets: Optional[set[str]] = None,
                   final: bool = False) -> Optional[dict]:
    """The decision in the lead's reply, or None if there is no valid one. ``targets`` are the
    steps rework may send back; without them, rework is not a valid answer. ``final``: every step
    has finished, and only done, rework and replan are."""
    start, end = (text or "").find("{"), (text or "").rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        payload = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    allowed = FINAL_ACTIONS if final else tuple(action for action in ACTIONS if action != "done")
    if not isinstance(payload, dict) or payload.get("action") not in allowed:
        return None
    clean = {key: str(payload[key]).strip() for key in ("action", "reason", "instructions", "model_id",
                                                         "question", "user_action") if payload.get(key)}
    options = payload.get("options")
    clean["options"] = [str(item).strip()[:300] for item in options if str(item).strip()][:4] if isinstance(options, list) else []
    if clean["action"] == "switch_model" and clean.get("model_id") not in model_ids:
        return None
    if clean["action"] == "ask_user" and not clean.get("question"):
        return None
    if clean["action"] == "rework":
        chosen = payload.get("targets") or sorted(targets or ())
        if not targets or not isinstance(chosen, list) or not set(map(str, chosen)) <= targets:
            return None
        clean["targets"] = [str(item) for item in chosen]
    if clean["action"] == "replan":
        plan = payload.get("plan")
        if not isinstance(plan, dict) or not any(plan.get(key) for key in ("add", "remove", "update")):
            return None
        clean["plan"] = plan
    return clean
