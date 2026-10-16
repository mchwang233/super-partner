"""What happens after an attempt of a step fails when the task's lead does not decide (no lead
is available, or it sent a diagnosis to repair something): which model tries next, and what a
diagnosis means for the step. Decisions only; executor.py carries them out. With a lead, its
decisions (leading.py) take the place of these rules."""

from __future__ import annotations

from typing import Optional

from .config import settings
from .models import DiagnosisStatus, ModelProfile, Outcome, TaskNode
from .routing import stronger_than, strongest_for

# Turns of tool calls an API model gets for a node, as a multiple of api_agent_max_turns.
TURNS_BY_DIFFICULTY = {"easy": 1.0, "normal": 1.5, "hard": 2.0}
# Outcomes that say a model's attempt was not good enough. Provider limits (quota, unavailable),
# budget pauses, cancelled or superseded work, unverified reviews and unable steps do not.
MODEL_FAILURES = {Outcome.REJECTED, Outcome.VERIFY_FAILED, Outcome.ERROR}
# Failures in a row of one model on a step, after which the step goes to another model.
SWITCH_AFTER = 2
# Environment repairs per node that do not count as failed attempts.
MAX_ENVIRONMENT_FIXES = 2


def turns_for(node: TaskNode) -> Optional[int]:
    """Turns of tool calls for an attempt, or None without a limit (the default)."""
    if not settings.api_agent_max_turns:
        return None
    return max(1, round(settings.api_agent_max_turns * TURNS_BY_DIFFICULTY.get(node.difficulty, 1.0)))


def recent_failures(node: TaskNode) -> list:
    """The step's model failures since its last accepted attempt, oldest first. Running out of
    turns is one too, whatever the model claimed at the end."""
    failures = []
    for attempt in node.attempts:
        if attempt.outcome == Outcome.PASSED:
            failures = []
        elif attempt.outcome in MODEL_FAILURES or attempt.turn_limit:
            failures.append(attempt)
    return failures


def failed_models(node: TaskNode) -> set[str]:
    """Models that failed the step SWITCH_AFTER times in a row since it last passed."""
    failed, streak, previous = set(), 0, None
    for attempt in recent_failures(node):
        streak = streak + 1 if attempt.model_id == previous else 1
        previous = attempt.model_id
        if streak >= SWITCH_AFTER and attempt.model_id:
            failed.add(attempt.model_id)
    return failed


def retry_model(node: TaskNode, models: list[ModelProfile], configured: set[str],
                needs_vision: bool = False) -> Optional[ModelProfile]:
    """The model for the step's next attempt: the strongest one for it, unless that one already
    failed it twice in a row; then the best other model, a membership CLI included."""
    excluded = failed_models(node)
    pick = strongest_for(node, models, configured, needs_vision)
    if pick is None or pick.id in excluded:
        pick = stronger_than(node, models, configured, None, needs_vision, exclude=excluded) or pick
    return pick


def after_diagnosis(outcome: str, status: Optional[str], *, has_model: bool, environment_fixes: int) -> str:
    """What a diagnosis of a failed attempt means for the step.

    ``outcome`` is the attempt's, ``status`` the diagnosis's (None if it did not finish). Returns
    one of: ``hold_for_platform``, ``ask_user``, ``switch_model``, ``retry_fixed`` (the
    environment was repaired; the failure does not count), ``unable``, ``pause``,
    ``pause_rework`` (a verification out of rework rounds), ``retry``.
    """
    if status == DiagnosisStatus.PLATFORM_PENDING:
        return "hold_for_platform"
    if status == DiagnosisStatus.NEEDS_INPUT:
        return "ask_user"
    if (status in {DiagnosisStatus.QUOTA_EXHAUSTED, DiagnosisStatus.MODEL_UNAVAILABLE}
            and outcome == Outcome.ERROR and has_model):
        return "switch_model"
    if status == DiagnosisStatus.FIXED and environment_fixes < MAX_ENVIRONMENT_FIXES:
        return "retry_fixed"
    if status == DiagnosisStatus.UNABLE or (outcome == Outcome.UNABLE and status != DiagnosisStatus.RETRY):
        return "unable"
    if status == DiagnosisStatus.BLOCKED:
        return "pause"
    if outcome == Outcome.VERIFY_FAILED:
        return "pause_rework"
    return "retry"
