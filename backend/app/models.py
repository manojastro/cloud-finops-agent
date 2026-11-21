from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class RiskLevel(str, Enum):
    INFO = "INFO"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class WorkflowState(str, Enum):
    DETECTED = "DETECTED"
    INVESTIGATED = "INVESTIGATED"
    VALIDATED = "VALIDATED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    REAPPROVAL_REQUIRED = "REAPPROVAL_REQUIRED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    APPLIED = "APPLIED"
    VERIFIED = "VERIFIED"
    ROLLED_BACK = "ROLLED_BACK"
    ROLLBACK_FAILED = "ROLLBACK_FAILED"


class Evidence(BaseModel):
    id: str
    kind: str
    claim: str
    observed_value: float | str | bool
    unit: str | None = None
    source: str
    captured_at: str


class Hypothesis(BaseModel):
    description: str
    confidence: float = Field(ge=0, le=1)
    supporting_evidence_ids: list[str]


class OptimizationPlan(BaseModel):
    plan_id: str
    label: str
    normal_instance_type: str
    peak_instance_type: str
    normal_hours: float
    peak_hours: float
    scheduled: bool
    recurring_peak: bool
    peak_day: int | None = Field(default=None, ge=1, le=31)
    projected_monthly_cost_usd: float
    estimated_monthly_savings_usd: float


class Action(BaseModel):
    action_type: str
    description: str
    risk: RiskLevel
    blast_radius: str
    reversibility: str
    expected_monthly_savings_usd: float = Field(ge=0)
    confidence: float = Field(ge=0, le=1)
    required_approval: bool
    rollback_plan: str
    parameters: OptimizationPlan


class RCAResult(BaseModel):
    incident_id: str
    probable_root_cause: str
    confidence: float = Field(ge=0, le=1)
    hypotheses: list[Hypothesis]
    evidence: list[Evidence]
    recommended_actions: list[Action]


class ShadowMetric(BaseModel):
    name: str
    projected_value: float
    threshold: float
    comparison: Literal["lte", "gte"]
    passed: bool
    unit: str


class ValidationResult(BaseModel):
    validation_id: str
    recommendation_id: str
    scenario: str
    days_simulated: int
    passed: bool
    metrics: list[ShadowMetric]
    alignment_checks: dict[str, bool]
    plan_sha256: str
    terraform_sha256: str
    blocked_alternative: dict[str, Any]
    summary: str


class ApprovalRequest(BaseModel):
    decision: Literal["APPROVE", "REJECT"]
    reason: str = Field(min_length=3, max_length=500)


class DemoRunResponse(BaseModel):
    incident_id: str
    recommendation_id: str
    state: WorkflowState
    rca: RCAResult
    validation: ValidationResult
    terraform_proposal: str


class ExecutionResult(BaseModel):
    execution_id: str
    recommendation_id: str
    mode: Literal["SIMULATION"] = "SIMULATION"
    status: Literal["SUCCEEDED", "ROLLED_BACK", "BLOCKED"]
    realized_monthly_savings_usd: float
    slo_impact: str
    rollback_available: bool
    message: str
    estimated_monthly_savings_usd: float = 0.0
    observed_post_state: dict[str, float | str | bool] = Field(default_factory=dict)
    change_observation: dict[str, float | str | bool] = Field(default_factory=dict)
    rollback_observation: dict[str, float | str | bool] = Field(default_factory=dict)
    rollback_verified: bool = False


class AgentDefinition(BaseModel):
    name: str
    responsibility: str
    inputs: list[str]
    outputs: list[str]
    tools: list[str]
    permission_boundary: str
    failure_behavior: str
