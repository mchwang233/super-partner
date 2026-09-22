from __future__ import annotations

from datetime import datetime, timezone
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
    context_window: int
    input_per_million: float
    output_per_million: float
    cached_input_per_million: Optional[float] = None
    supports_tools: bool = True
    supports_vision: bool = False
    scores: CapabilityScore
    source_url: Optional[str] = None
    effective_at: datetime = Field(default_factory=utcnow)
    pricing_verified: bool = False
    channel: str = "api"
    execution_priority: int = 100


class TaskNode(BaseModel):
    id: str
    title: str
    instructions: str
    capability: str
    dependencies: list[str] = Field(default_factory=list)
    estimated_input_tokens: int = 1500
    estimated_output_tokens: int = 800
    quality_threshold: float = 0.65
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
