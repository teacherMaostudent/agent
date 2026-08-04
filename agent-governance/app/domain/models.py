from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utc_now() -> datetime:
    return datetime.now(UTC)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Severity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class FindingStatus(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"


class Identity(StrictModel):
    tenant_id: str
    user_id: str
    roles: frozenset[str] = frozenset()

    @field_validator("roles", mode="before")
    @classmethod
    def normalize_roles(cls, value: Any) -> frozenset[str]:
        if value is None:
            return frozenset()
        if isinstance(value, str):
            return frozenset(part.strip() for part in value.split(",") if part.strip())
        return frozenset(value)


class GovernanceEvent(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    event_id: str = Field(min_length=1, max_length=160)
    source_service: str = Field(min_length=1, max_length=100)
    event_type: str = Field(min_length=1, max_length=150)
    trace_id: str = Field(min_length=1, max_length=160)
    tenant_id: str = Field(min_length=1, max_length=100)
    occurred_at: datetime
    payload: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_canonical_payload(self) -> GovernanceEvent:
        required = {
            "agent.run.completed": {"run_id", "agent_id", "status"},
            "agent.run.interrupted": {"run_id", "agent_id", "status"},
            "tool.execution.completed": {
                "tool_name",
                "tool_version",
                "status",
                "risk",
                "approval_granted",
            },
            "llm.request.completed": {
                "request_id",
                "model",
                "data_region",
                "cost",
                "cost_currency",
                "success",
            },
        }.get(self.event_type)
        if required:
            missing = sorted(required - self.payload.keys())
            if missing:
                raise ValueError(
                    f"{self.event_type} payload is missing canonical fields: {missing}"
                )
        return self


class AuditEvent(GovernanceEvent):
    sequence: int
    received_at: datetime
    previous_hash: str = ""
    event_hash: str = ""


class TenantPolicy(StrictModel):
    tenant_id: str
    allowed_models: list[str] = Field(default_factory=list)
    allowed_data_regions: list[str] = Field(default_factory=list)
    require_evidence_for_answer: bool = False
    require_approval_for_high_risk_tools: bool = True
    max_run_cost_usd: float | None = Field(default=None, gt=0, le=10_000)
    max_run_latency_ms: int | None = Field(default=None, ge=1, le=86_400_000)
    updated_by: str = "system"
    updated_at: datetime = Field(default_factory=utc_now)


class TenantPolicyUpdate(StrictModel):
    allowed_models: list[str] = Field(default_factory=list)
    allowed_data_regions: list[str] = Field(default_factory=list)
    require_evidence_for_answer: bool = False
    require_approval_for_high_risk_tools: bool = True
    max_run_cost_usd: float | None = Field(default=None, gt=0, le=10_000)
    max_run_latency_ms: int | None = Field(default=None, ge=1, le=86_400_000)


class Finding(StrictModel):
    finding_id: str
    tenant_id: str
    event_id: str
    rule_id: str
    severity: Severity
    status: FindingStatus
    subject_type: str
    subject_id: str
    summary: str
    evidence: dict[str, Any]
    created_at: datetime
    resolved_at: datetime | None = None
    resolved_by: str | None = None
    resolution_note: str | None = None


class FindingResolution(StrictModel):
    note: str = Field(default="", max_length=2_000)


class IngestionResult(StrictModel):
    accepted: bool
    duplicate: bool
    finding_ids: list[str] = Field(default_factory=list)


class AuditEventList(StrictModel):
    items: list[AuditEvent]
    next_cursor: int | None = None


class FindingList(StrictModel):
    items: list[Finding]
    next_cursor: str | None = None


class ComplianceReport(StrictModel):
    tenant_id: str
    from_time: datetime | None
    to_time: datetime | None
    total_events: int
    events_by_source: dict[str, int]
    findings_by_severity: dict[Severity, int]
    open_findings: int
    compliance_status: Literal["compliant", "attention", "violation"]


class HealthStatus(StrictModel):
    status: Literal["ok"]
    service: Literal["agent-governance"] = "agent-governance"
