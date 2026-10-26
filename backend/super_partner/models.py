from __future__ import annotations

from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Literal, Optional
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator, model_validator


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# Task difficulty sets the minimum routing score (0-100) for the task's capability.
DIFFICULTY_THRESHOLDS = {"easy": 70, "normal": 80, "hard": 90}


def difficulty_for(threshold: float) -> str:
    """Difficulty implied by a bare threshold, for plans saved before difficulty existed."""
    return "hard" if threshold >= 88 else "normal" if threshold >= 78 else "easy"


def to_percent_scale(value: float) -> float:
    """Scores and thresholds are 0-100; records saved before that used 0-1."""
    return round(value * 100, 1) if 0 < value <= 1 else value


class RunStatus(str, Enum):
    DRAFT = "draft"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    # Everything that could be done is done, but some steps cannot be done here (no network,
    # missing credentials or permissions); each such step says why.
    INCOMPLETE = "incomplete"


# A task in one of these states has stopped and can be resumed, changed or deleted.
FINISHED = {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.INCOMPLETE}


class NodeStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"
    # The step's goal cannot be reached in this environment; unable_reason says why.
    UNABLE = "unable"


class RiskLevel(str, Enum):
    SAFE = "safe"
    REVIEW = "review"
    RESTRICTED = "restricted"


class CapabilityScore(BaseModel):
    """Routing scores on a 0-100 scale."""
    coding: float = 50
    planning: float = 50
    research: float = 50
    chinese: float = 50
    tool_use: float = 50
    structured_output: float = 50
    reliability: float = 50

    _upgrade_scale = field_validator("*")(to_percent_scale)

    def score_for(self, capability: str) -> float:
        return float(getattr(self, capability, self.reliability))

    @property
    def overall(self) -> float:
        """The mean of all scores: how capable the model is in general."""
        return (self.coding + self.planning + self.research + self.chinese + self.tool_use
                + self.structured_output + self.reliability) / 7


class ModelProfile(BaseModel):
    id: str
    provider: str
    display_name: str
    enabled: bool = True
    context_window: Optional[int] = Field(default=None, gt=0)
    # Most output tokens one request may ask for; each API call asks for this much.
    max_output_tokens: Optional[int] = Field(default=None, gt=0)
    input_per_million: Optional[float] = Field(default=None, ge=0, allow_inf_nan=False)
    output_per_million: Optional[float] = Field(default=None, ge=0, allow_inf_nan=False)
    cached_input_per_million: Optional[float] = None
    supports_tools: Optional[bool] = None
    supports_vision: Optional[bool] = None
    supports_reasoning: Optional[bool] = None
    supports_structured_output: Optional[bool] = None
    scores: Optional[CapabilityScore] = None
    # "artificial_analysis" when derived from AA, "none" without AA results (not routed);
    # the offline demo model keeps fixed scores and the source "demo".
    scores_source: str = "none"
    scores_basis: Optional[str] = None
    source_url: Optional[str] = None
    effective_at: datetime = Field(default_factory=utcnow)
    pricing_verified: bool = False
    channel: str = "api"
    execution_priority: int = 100
    description: Optional[str] = None
    release_date: Optional[date] = None
    catalog_source: str = "builtin"
    catalog_status: str = "listed"
    catalog_checked_at: Optional[datetime] = None
    discovered_at: datetime = Field(default_factory=utcnow)
    pricing_source: str = "builtin"
    available_via_api: Optional[bool] = None
    api_checked_at: Optional[datetime] = None

    @property
    def routing_ready(self) -> bool:
        return (
            self.scores is not None
            and self.context_window is not None
            and self.input_per_million is not None
            and self.output_per_million is not None
            and self.catalog_status not in {"deprecated", "retired"}
            and self.available_via_api is not False
        )


class ToolCall(BaseModel):
    """One API or CLI tool call at work: in which turn, which tool, on what, and whether
    it went through (a command exiting 0, a tool without an error)."""
    turn: int
    tool: str
    detail: str = ""
    # Missing completion evidence is not a successful tool call (including old records).
    ok: Optional[bool] = None
    id: Optional[str] = None
    exit_code: Optional[int] = None
    running: bool = False
    status: Literal["succeeded", "failed", "running", "incomplete", "unknown"] = "unknown"
    diagnostic: Optional[str] = None
    # The start of what the tool returned, as the model saw it.
    output: str = ""

    @model_validator(mode="before")
    @classmethod
    def legacy_status(cls, value):
        if isinstance(value, dict) and "status" not in value:
            value = dict(value)
            value["status"] = ("running" if value.get("running") else "succeeded" if value.get("ok") is True
                               else "failed" if value.get("ok") is False else "unknown")
        return value


class Outcome:
    """How an attempt of a step ended (``NodeAttempt.outcome``)."""
    PASSED = "passed"
    DELEGATED = "delegated"       # yielded to accepted child work; not a failure
    REJECTED = "rejected"            # the reviewer did not accept the result
    VERIFY_FAILED = "verify_failed"  # a verification step found problems
    UNVERIFIED = "unverified"        # no reviewer could give a verdict
    UNABLE = "unable"                # the step cannot be done in this environment
    ERROR = "error"                  # the model call failed
    BUDGET = "budget"                # not enough budget left for the next request
    SUPERSEDED = "superseded"        # upstream rework made the attempt stale
    CANCELLED = "cancelled"
    QUOTA = "quota"                  # the model ran out of usage; another takes over
    UNAVAILABLE = "unavailable"      # the model's service is down; another takes over
    BLOCKED = "blocked"              # missing artifact evidence or an unenforceable contract
    STOPPED = "stopped"              # a declared work-package limit was reached


class DiagnosisStatus:
    """A diagnosis's conclusion (``Diagnosis.verdict``), from its STATUS line or the platform repair."""
    FIXED = "fixed"
    RETRY = "retry"
    UNABLE = "unable"
    BLOCKED = "blocked"
    NEEDS_INPUT = "needs_input"
    QUOTA_EXHAUSTED = "quota_exhausted"
    MODEL_UNAVAILABLE = "model_unavailable"
    PLATFORM_REPAIR = "platform_repair"
    PLATFORM_PENDING = "platform_pending"
    PLATFORM_FIXED = "platform_fixed"
    PLATFORM_FAILED = "platform_failed"


class NodeAttempt(BaseModel):
    """One run of a node: what model did it, and whether its result was accepted."""
    # Older records have no trustworthy wall-clock duration.
    duration_seconds: Optional[float] = None
    # Never infer an attempt's answer from TaskNode.result: it may be from an older attempt.
    result: Optional[str] = None
    result_source: Optional[str] = None
    interrupted: bool = False
    model_id: Optional[str] = None
    # One of Outcome.
    outcome: str
    reason: Optional[str] = None
    reviewer_model_id: Optional[str] = None
    cost_usd: float = 0.0
    # API models acting through tools: turns used, whether they ran out (the result is then the
    # model's progress summary), and every tool call.
    turns: int = 0
    turn_limit: bool = False
    tool_calls: list[ToolCall] = Field(default_factory=list)
    # Every tool call with its whole output, one JSON line each (tool_calls keeps only the start
    # of the first ones): a file under the data folder, for looking back at what happened.
    trace: Optional[str] = None
    finished_at: datetime = Field(default_factory=utcnow)

    @field_validator("tool_calls")
    @classmethod
    def ended_calls(cls, calls):
        # A persisted attempt has ended even when its last CLI event said "running".
        return [call.model_copy(update={"ok": None, "running": False, "status": "incomplete",
                                       "diagnostic": call.diagnostic or "No completion event was recorded; exit status is unknown."})
                if call.running or call.status == "running" else call for call in calls]


class NodeInput(BaseModel):
    """What a node received from one upstream node."""
    node_id: str
    form: str  # full | summary | truncated
    changed_files: int = 0


class Artifact(BaseModel):
    """A named file or directory, relative to the step's workspace."""
    path: str = Field(min_length=1)
    kind: Literal["file", "directory"] = "file"


class ArtifactInput(Artifact):
    # External evidence must already exist. Dependency evidence must also have been accepted.
    source: Literal["external", "dependency"]


class ArtifactContract(BaseModel):
    stage: Optional[Literal["coverage_vdb", "urg_report", "delivery_audit"]] = None
    inputs: list[ArtifactInput]
    outputs: list[Artifact] = Field(min_length=1)
    acceptance_commands: list[str] = Field(min_length=1)
    blocked_when: list[str] = Field(min_length=1)
    max_turns: int = Field(gt=0, strict=True)
    max_cost_usd: float = Field(gt=0, allow_inf_nan=False)

    @field_validator("acceptance_commands", "blocked_when")
    @classmethod
    def nonempty(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values):
            raise ValueError("contract declarations must not be blank")
        return values


class ArtifactEvidence(BaseModel):
    generation: int
    inputs: dict[str, str] = Field(default_factory=dict)
    outputs: dict[str, str] = Field(default_factory=dict)
    checks: list[dict] = Field(default_factory=list)
    accepted: bool = False


class TaskNode(BaseModel):
    id: str
    title: str
    instructions: str
    capability: str
    dependencies: list[str] = Field(default_factory=list)
    estimated_input_tokens: int = 1500
    estimated_output_tokens: int = 800
    # Minimum routing score (0-100) for the node's capability. Normally set from difficulty;
    # it can be lowered at planning time when no available model reaches it.
    quality_threshold: float = 65
    # easy/normal: cheapest qualifying model (membership CLIs first); hard: highest score.
    difficulty: str = "normal"
    # True when the node must read or change files in the run's project directory.
    needs_workspace: bool = False
    # The step as a small work package (planner.py): the files it creates or changes, the files
    # it only reads, what it delivers to later steps (names, signatures, endpoints), and a
    # command that shows it works. A scoped step gets just these files, not the whole project.
    files: list[str] = Field(default_factory=list)
    reads: list[str] = Field(default_factory=list)
    provides: str = ""
    check: str = ""
    artifact_contract: Optional[ArtifactContract] = None
    artifact_evidence: Optional[ArtifactEvidence] = None
    # Persisted at each request, never reset by automatic retry, rework or a model switch.
    contract_turns: int = 0
    # Individual worker tool invocations, reserved before side effects (including failures).
    contract_tool_calls: int = 0
    contract_cost_usd: float = 0.0
    contract_generation: Optional[int] = None
    artifact_baseline: dict[str, str] = Field(default_factory=dict)
    artifact_inputs: dict[str, str] = Field(default_factory=dict)
    # The innermost plan part (PlanPart) this task belongs to; None in a plan without parts.
    part_id: Optional[str] = None
    employee_id: Optional[str] = None
    delegation_parent_id: Optional[str] = None
    delegation_request_id: Optional[str] = None
    delegation_payload_hash: str = ""
    # Major parts whose finished work this task runs or tests, so it waits for them (outline.link).
    # Other tasks build against what those parts provide and start at once.
    after_parts: list[str] = Field(default_factory=list)
    model_id: Optional[str] = None
    # Why the router chose model_id, for the plan view.
    routing_note: Optional[str] = None
    # When the current (or last) attempt started; attempts record when each one ended.
    started_at: Optional[datetime] = None
    estimated_cost_usd: float = 0.0
    status: NodeStatus = NodeStatus.PENDING
    result: Optional[str] = None
    # How a reviewer decides whether the result is acceptable.
    acceptance_criteria: str = ""
    # Upstream nodes this verification node checks; a failed verdict sends them back for rework.
    verifies: list[str] = Field(default_factory=list)
    attempts: list[NodeAttempt] = Field(default_factory=list)
    # Failed attempts since the node was last (re)started; retries stop at max_node_attempts.
    failed_attempts: int = 0
    # Environment repairs after this node ran out of attempts; reset when the user resumes.
    environment_fixes: int = 0
    # What the next attempt must fix: a reviewer's or a verification node's findings.
    feedback: Optional[str] = None
    review_passed: Optional[bool] = None
    review: Optional[str] = None
    # Why the step cannot be done here, and what would make it possible.
    unable_reason: Optional[str] = None
    # Short form of a long result, handed to downstream nodes instead of the full text.
    summary: Optional[str] = None
    changed_files: list[str] = Field(default_factory=list)
    inputs: list[NodeInput] = Field(default_factory=list)
    # Rework invalidates in-flight results that were based on an older generation.
    generation: int = 0

    _upgrade_threshold = field_validator("quality_threshold")(to_percent_scale)

    @model_validator(mode="before")
    @classmethod
    def _derive_difficulty(cls, data: Any) -> Any:
        if isinstance(data, dict) and "difficulty" not in data and "quality_threshold" in data:
            data = {**data, "difficulty": difficulty_for(to_percent_scale(float(data["quality_threshold"])))}
        return data


class CostEstimate(BaseModel):
    base_usd: float
    retry_reserve_usd: float
    tool_reserve_usd: float
    low_usd: float
    high_usd: float
    hard_limit_usd: float


class PlanPart(BaseModel):
    """A part of a plan (大项 at the top level), split level by level into smaller parts and
    finally tasks (outline.py). Only tasks run; parts group them. A task is planned as major
    parts only; once it runs, every major part is split at the same time (refining.py)."""
    id: str
    title: str
    goal: str = ""
    acceptance: str = ""
    parent_id: Optional[str] = None
    employee_id: Optional[str] = None
    # Earlier parts or tasks under the same parent that this part waits for.
    dependencies: list[str] = Field(default_factory=list)
    # What other parts may rely on (names, signatures, endpoints, file formats): parts are split
    # apart, so this is all they know of each other.
    provides: str = ""
    # The lead's guess of its tasks and their main capability, for the estimate before it is split.
    size: int = 0
    capability: str = "coding"
    difficulty: Literal["easy", "normal", "hard"] = "normal"
    refinement_effort: Literal["", "low", "medium", "high", "max"] = ""
    refinement_reason: str = ""
    estimated_cost_usd: float = 0.0
    # A major part: planned (not split yet), refining, ready (split into tasks) or failed.
    status: str = "ready"
    error: Optional[str] = None


class PreflightCheck(BaseModel):
    """One check before a task's first step: what it is, the command, and how it went. A
    blocking one that fails stops the task until it is repaired or the user goes on anyway."""
    name: str
    command: str = ""
    ok: bool = True
    blocking: bool = True
    output: str = ""


class Preflight(BaseModel):
    checked_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    passed: bool = True
    checks: list[PreflightCheck] = Field(default_factory=list)
    # A repair was tried for the failed checks; the task was paused for the user over them.
    repaired: bool = False
    paused: bool = False
    # This machine's skills the steps will be given: prepared now, or kept from earlier tasks.
    skills: list[str] = Field(default_factory=list)


class Employee(BaseModel):
    """A project-scoped responsibility and model, chosen before work is split."""
    id: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")
    name: str = Field(min_length=1, max_length=80)
    role: str = Field(min_length=1, max_length=2000)
    model_id: str
    capabilities: list[str] = Field(min_length=1, max_length=6)
    skills: list[str] = Field(default_factory=list, max_length=6)
    skill_tools: list[str] = Field(default_factory=list, max_length=12)
    skill_context: str = ""
    reason: str = Field(default="", max_length=1000)


class PlanChoice(BaseModel):
    """A scope decision the request left open that changes the work a lot, decided by the lead
    while planning and shown to the user before confirming (planner.CHOICES_RULE)."""
    question: str
    assumed: str
    alternative: str = ""


class TaskPlan(BaseModel):
    summary: str
    nodes: list[TaskNode]
    parts: list[PlanPart] = Field(default_factory=list)
    employees: list[Employee] = Field(default_factory=list, max_length=12)
    team_brief: str = ""
    # What the plan was made from (request, project context and brief), for splitting its parts
    # later; and whether its steps work in a project folder.
    request: str = ""
    in_project: bool = False
    # Commands that show the tools, licenses and services the task needs work here, from the
    # lead's outline; run before the first step (preflight.py).
    requires: list[str] = Field(default_factory=list)
    # A few English words naming the task, for its folder (OUTLINE_PROMPT "folder").
    folder: str = ""
    estimate: Optional[CostEstimate] = None
    planning_method: str = "heuristic"
    planner_model_id: Optional[str] = None
    planning_cost_usd: float = 0.0
    planning_note: Optional[str] = None
    choices: list[PlanChoice] = Field(default_factory=list)

    def unsplit_cost(self) -> float:
        """The estimate of major parts not yet split into tasks."""
        return sum(part.estimated_cost_usd for part in self.parts if not part.parent_id and part.status != "ready")

    def part_path(self, part_id: Optional[str]) -> list[PlanPart]:
        """The parts from the top level down to ``part_id``."""
        by_id = {part.id: part for part in self.parts}
        path = []
        while part_id in by_id and len(path) < 10:
            path.insert(0, by_id[part_id])
            part_id = by_id[part_id].parent_id
        return path


class Attachment(BaseModel):
    """A file the user attached to a task; ``kind`` is detected from its content."""
    id: str
    name: str
    size: int
    kind: str  # "image" (png/jpeg/gif/webp), "text" (UTF-8) or "file" (other binary)
    media_type: str


class DiagnosisMessage(BaseModel):
    text: str
    choice: Optional[str] = None
    created_at: datetime = Field(default_factory=utcnow)


class Diagnosis(BaseModel):
    """A strong model's analysis of why a task is stuck, with advice for the next attempt."""
    id: str
    # manual (the 诊断 button), auto (a node ran out of attempts), lead (the lead's question to
    # the user) or lead_repair (a repair the lead asked for, under its decision ``decision_id``)
    trigger: str
    node_id: Optional[str] = None
    decision_id: Optional[str] = None
    model_id: Optional[str] = None
    status: str = "running"  # running | done | failed
    report: Optional[str] = None
    # What the failed node's next attempt should do differently; resuming hands it over.
    advice: Optional[str] = None
    # Automatic diagnoses after a failed attempt: fixed (environment repaired, run again),
    # retry (nothing outside the task is wrong), quota_exhausted (switch execution model),
    # or blocked (the user must act).
    verdict: Optional[str] = None  # one of DiagnosisStatus
    # A concrete human-only dependency is required before a BLOCKED diagnosis pauses a task.
    user_action: Optional[str] = None
    scope: str = "environment"  # task | environment | super_partner
    repair_id: Optional[str] = None
    reply_to: Optional[str] = None
    question: Optional[str] = None
    choices: list[str] = Field(default_factory=list)
    messages: list[DiagnosisMessage] = Field(default_factory=list)
    error: Optional[str] = None
    cost_usd: float = 0.0
    created_at: datetime = Field(default_factory=utcnow)
    finished_at: Optional[datetime] = None


class Decision(BaseModel):
    """What the task's lead decided, why, and about what (supervisor.py)."""
    id: str = Field(default_factory=lambda: uuid4().hex[:8])
    node_id: Optional[str] = None
    # What it decided on: attempt_failed | verify_failed | user_answer | repair_done | engineer_done |
    # all_done.
    event: str
    # retry | switch_model | accept | unable | ask_user | repair_environment | repair_platform |
    # request_component (engineer_job_id), rework, replan, done,
    # or fallback when no lead could decide and the usual diagnosis took over.
    action: str
    reason: str = ""
    instructions: Optional[str] = None
    model_id: Optional[str] = None
    # Steps a rework sent back.
    targets: list[str] = Field(default_factory=list)
    lead_model_id: Optional[str] = None
    engineer_job_id: Optional[str] = None
    created_at: datetime = Field(default_factory=utcnow)


class LeadThinking(BaseModel):
    """The lead is deciding something right now, shown until its decision arrives."""
    # attempt_failed | verify_failed | user_answer | repair_done | engineer_done | overrun | all_done
    event: str
    node_id: Optional[str] = None
    started_at: datetime = Field(default_factory=utcnow)


class LeadMessage(BaseModel):
    """The user talking with the task's lead outside its decisions."""
    id: str = Field(default_factory=lambda: uuid4().hex[:8])
    role: str  # user | lead
    text: str = ""
    status: str = "done"  # pending (the lead is answering) | done | failed
    # The platform engineer's job the lead brought into the conversation with this answer.
    engineer_job_id: Optional[str] = None
    created_at: datetime = Field(default_factory=utcnow)


class SupervisorSession(BaseModel):
    """The task's lead: the strong model that planned it keeps one conversation until the task
    ends (supervisor.py, docs/design/supervisor.md)."""
    model_id: Optional[str] = None
    # A dedicated refinement conversation may override the main lead's effort independently.
    reasoning_effort: Optional[str] = None
    # A membership CLI's own conversation id, continued on every call.
    session_id: Optional[str] = None
    # The folder the CLI conversation lives in; it must stay the same for it to be continued.
    cwd: Optional[str] = None
    # An API model's conversation: [{"role": "user" | "assistant", "content": text}].
    history: list[dict] = Field(default_factory=list)
    calls: int = 0
    # Models that led before (their conversation was lost or they ran out of quota).
    previous: list[str] = Field(default_factory=list)
    started_at: datetime = Field(default_factory=utcnow)


class EngineerMessage(BaseModel):
    """One line in a platform engineer job's conversation."""
    role: str  # user | engineer | system
    text: str
    created_at: datetime = Field(default_factory=utcnow)


class EngineerJob(BaseModel):
    """A change to Super Partner itself by the platform engineer (engineer.py,
    docs/design/platform-engineer.md): a repair, or a component to add."""
    id: str = Field(default_factory=lambda: uuid4().hex[:10])
    kind: str = "component"  # repair | component
    origin: str = "user"  # user | lead | diagnosis
    title: str = ""
    request: str
    # queued | planning | plan_ready | editing | validating | ready | needs_input | applying, or
    # one of ENGINEER_DONE; ``phase`` is the work a queued job waits to do (plan | edit).
    status: str = "queued"
    phase: str = "plan"
    plan: Optional[str] = None
    report: Optional[str] = None
    question: Optional[str] = None
    choices: list[str] = Field(default_factory=list)
    # The task a lead or diagnosis started it from.
    run_id: Optional[str] = None
    node_id: Optional[str] = None
    decision_id: Optional[str] = None
    diagnosis_id: Optional[str] = None
    improvement_id: Optional[str] = None
    # The isolated source copy (platform_repair.PlatformRepairs job id), and what it changed.
    repair_id: Optional[str] = None
    files: list[str] = Field(default_factory=list)
    checks: list[str] = Field(default_factory=list)
    diff: Optional[str] = None
    commit: Optional[str] = None
    model_id: Optional[str] = None
    messages: list[EngineerMessage] = Field(default_factory=list)
    budget_usd: float = 2.0
    cost_usd: float = 0.0
    error: Optional[str] = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    finished_at: Optional[datetime] = None


# Engineer jobs in these states are over; those in WAITING wait for the user.
ENGINEER_DONE = {"applied", "failed", "rejected", "cancelled"}
ENGINEER_WAITING = {"plan_ready", "ready", "needs_input"}


class LearningSettings(BaseModel):
    enabled: bool = False
    budget_usd: float = Field(default=0.5, ge=0, le=100)


class LearningReview(BaseModel):
    run_id: str
    revision: str
    status: str = "queued"  # queued | analyzing | validating | done | blocked | failed | skipped
    # Old unfinished reviews must not become explicit manual requests merely on restart.
    automatic: bool = True
    conclusion: str = ""
    model_id: Optional[str] = None
    budget_usd: float = 0.5
    cost_usd: float = 0
    # The remaining budget handed to platform jobs, counted once, even across restarts.
    allocated_usd: float = 0
    error: Optional[str] = None
    created_at: datetime = Field(default_factory=utcnow)


class Improvement(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex[:12])
    run_id: str
    revision: str
    kind: Literal["skill", "rule", "platform"]
    title: str = Field(min_length=1, max_length=160)
    rationale: str = Field(min_length=1, max_length=2000)
    content: str = Field(min_length=1, max_length=7000)
    tools: list[str] = Field(default_factory=list)
    capability: Optional[str] = None
    source_nodes: list[str] = Field(default_factory=list)
    problem_kind: str = ""
    problem_key: str = ""
    check: str = Field(default="", max_length=4000)
    status: str = "proposed"  # proposed | validating | active | engineering | failed | disabled
    validation: dict = Field(default_factory=dict)
    baseline: dict = Field(default_factory=dict)
    engineer_job_id: Optional[str] = None
    applied_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=utcnow)


class ExecutionRun(BaseModel):
    id: str
    prompt: str
    status: RunStatus
    plan: TaskPlan
    budget_usd: float
    use_cli: bool = True
    # Commands may reach the internet (never this machine's local services); off by default.
    allow_network: bool = False
    project_dir: Optional[str] = None
    attachments: list[Attachment] = Field(default_factory=list)
    # Times a verification node sent work back; capped by max_rework_rounds.
    rework_rounds: int = 0
    # Repository the output was last published to.
    github_url: Optional[str] = None
    diagnoses: list[Diagnosis] = Field(default_factory=list)
    supervisor: Optional[SupervisorSession] = None
    decisions: list[Decision] = Field(default_factory=list)
    lead_messages: list[LeadMessage] = Field(default_factory=list)
    lead_thinking: Optional[LeadThinking] = None
    # Decisions counted against the limits only from here: the user continuing a task resets them.
    decisions_since: Optional[datetime] = None
    # Only tasks deliberately held for an application repair may resume after its restart.
    maintenance_id: Optional[str] = None
    # The platform engineer job (a component the lead asked for) this task waits for.
    engineer_job_id: Optional[str] = None
    # The status of each platform engineer job for this task when its lead last heard of it.
    engineer_seen: dict[str, str] = Field(default_factory=dict)
    waiting_diagnosis_id: Optional[str] = None
    # A requirement change continues a finished task: the task it builds on, the scratch folder
    # they share (when there is no project), and what the planner and steps are told about it.
    parent_run_id: Optional[str] = None
    workspace_id: Optional[str] = None
    # The readable name of its scratch folder (folders.task_folder); older tasks use their id.
    folder_name: Optional[str] = None
    background: Optional[str] = None
    # The check before the first step (preflight.py): its last result.
    preflight: Optional[Preflight] = None
    # Immutable content and baseline of the validated improvements actually supplied to this run.
    improvement_uses: list[dict] = Field(default_factory=list)
    improvements_prepared: bool = False
    actual_cost_usd: float = 0.0
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    final_result: Optional[str] = None
    error: Optional[str] = None

    @property
    def folder_id(self) -> str:
        """Name of the scratch folder under workspace_root; shared along a chain of changes."""
        return self.workspace_id or self.folder_name or self.id


class Event(BaseModel):
    id: Optional[int] = None
    run_id: str
    kind: str
    data: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)


class ApprovalRequest(BaseModel):
    id: str
    run_id: str
    action: str
    reason: str
    risk: RiskLevel
    approved: Optional[bool] = None
    created_at: datetime = Field(default_factory=utcnow)
