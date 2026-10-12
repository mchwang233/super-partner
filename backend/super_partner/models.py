from __future__ import annotations

from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RunStatus(str, Enum):
    DRAFT = "draft"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class NodeStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


class RiskLevel(str, Enum):
    SAFE = "safe"
    REVIEW = "review"
    RESTRICTED = "restricted"


class CapabilityScore(BaseModel):
    coding: float = 0.5
    planning: float = 0.5
    research: float = 0.5
    chinese: float = 0.5
    tool_use: float = 0.5
    structured_output: float = 0.5
    reliability: float = 0.5

    def score_for(self, capability: str) -> float:
        return float(getattr(self, capability, self.reliability))


class ModelProfile(BaseModel):
    id: str
    provider: str
    display_name: str
    enabled: bool = True
    context_window: Optional[int] = Field(default=None, gt=0)
    input_per_million: Optional[float] = Field(default=None, ge=0, allow_inf_nan=False)
    output_per_million: Optional[float] = Field(default=None, ge=0, allow_inf_nan=False)
    cached_input_per_million: Optional[float] = None
    supports_tools: Optional[bool] = None
    supports_vision: Optional[bool] = None
    supports_reasoning: Optional[bool] = None
    supports_structured_output: Optional[bool] = None
    scores: Optional[CapabilityScore] = None
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


class TaskNode(BaseModel):
    id: str
    title: str
    instructions: str
    capability: str
    dependencies: list[str] = Field(default_factory=list)
    estimated_input_tokens: int = 1500
    estimated_output_tokens: int = 800
    quality_threshold: float = 0.65
    # True when the node must read or change files in the run's project directory.
    needs_workspace: bool = False
    model_id: Optional[str] = None
    estimated_cost_usd: float = 0.0
    status: NodeStatus = NodeStatus.PENDING
    result: Optional[str] = None


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


class ExecutionRun(BaseModel):
    id: str
    prompt: str
    status: RunStatus
    plan: TaskPlan
    budget_usd: float
    use_cli: bool = True
    project_dir: Optional[str] = None
    actual_cost_usd: float = 0.0
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    final_result: Optional[str] = None
    error: Optional[str] = None


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
