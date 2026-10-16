from __future__ import annotations

from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Optional

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
    """One tool call of an API model at work: in which turn, which tool, on what, and whether
    it went through (a command exiting 0, a tool without an error)."""
    turn: int
    tool: str
    detail: str = ""
    ok: bool = True


class Outcome:
    """How an attempt of a step ended (``NodeAttempt.outcome``)."""
    PASSED = "passed"
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
    finished_at: datetime = Field(default_factory=utcnow)


class NodeInput(BaseModel):
    """What a node received from one upstream node."""
    node_id: str
    form: str  # full | summary | truncated
    changed_files: int = 0


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


class TaskPlan(BaseModel):
    summary: str
    nodes: list[TaskNode]
    estimate: Optional[CostEstimate] = None
    planning_method: str = "heuristic"
    planner_model_id: Optional[str] = None
    planning_cost_usd: float = 0.0
    planning_note: Optional[str] = None


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
    trigger: str  # manual (the 诊断 button) or auto (a node ran out of attempts)
    node_id: Optional[str] = None
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
    # Only tasks deliberately held for an application repair may resume after its restart.
    maintenance_id: Optional[str] = None
    waiting_diagnosis_id: Optional[str] = None
    # A requirement change continues a finished task: the task it builds on, the scratch folder
    # they share (when there is no project), and what the planner and steps are told about it.
    parent_run_id: Optional[str] = None
    workspace_id: Optional[str] = None
    background: Optional[str] = None
    actual_cost_usd: float = 0.0
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    final_result: Optional[str] = None
    error: Optional[str] = None

    @property
    def folder_id(self) -> str:
        """Name of the scratch folder under workspace_root; shared along a chain of changes."""
        return self.workspace_id or self.id


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
