from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utc_now() -> datetime:
    return datetime.now(UTC)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ToolRisk(StrEnum):
    READ_ONLY = "read_only"
    WRITE_LOW_RISK = "write_low_risk"
    WRITE_HIGH_RISK = "write_high_risk"
    HUMAN_APPROVAL_REQUIRED = "human_approval_required"


class InvocationStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    FAILED = "FAILED"


class ApprovalStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    CONSUMED = "CONSUMED"
    EXPIRED = "EXPIRED"


class HttpTransport(StrictModel):
    kind: Literal["http"] = "http"
    url: str = Field(min_length=8, max_length=2_000)
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = "POST"
    argument_location: Literal["json", "query"] = "json"
    static_headers: dict[str, str] = Field(default_factory=dict)
    auth_header: str | None = Field(default=None, max_length=100)
    auth_env: str | None = Field(default=None, max_length=200)
    allowed_hosts: list[str] = Field(min_length=1)

    @field_validator("allowed_hosts")
    @classmethod
    def normalize_hosts(cls, value: list[str]) -> list[str]:
        return sorted({item.strip().lower() for item in value if item.strip()})


class McpTransport(StrictModel):
    kind: Literal["mcp_streamable_http"]
    server_url: str = Field(min_length=8, max_length=2_000)
    remote_tool_name: str = Field(min_length=1, max_length=200)
    static_headers: dict[str, str] = Field(default_factory=dict)
    auth_header: str | None = Field(default=None, max_length=100)
    auth_env: str | None = Field(default=None, max_length=200)
    allowed_hosts: list[str] = Field(min_length=1)

    @field_validator("allowed_hosts")
    @classmethod
    def normalize_hosts(cls, value: list[str]) -> list[str]:
        return sorted({item.strip().lower() for item in value if item.strip()})


ToolTransport = HttpTransport | McpTransport


class ToolSpec(StrictModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,149}$")
    version: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=2_000)
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None = None
    required_permissions: list[str] = Field(default_factory=list)
    risk: ToolRisk = ToolRisk.READ_ONLY
    approval_required: bool = False
    enabled_tenants: list[str] = Field(default_factory=lambda: ["*"])
    idempotent: bool = True
    timeout_seconds: float = Field(default=20, gt=0, le=600)
    retry_attempts: int = Field(default=1, ge=1, le=5)
    rate_limit_per_minute: int = Field(default=60, ge=1, le=100_000)
    breaker_failure_threshold: int = Field(default=5, ge=1, le=100)
    breaker_reset_seconds: float = Field(default=30, gt=0, le=3_600)
    transport: ToolTransport = Field(discriminator="kind")

    @field_validator("required_permissions", "enabled_tenants")
    @classmethod
    def unique_values(cls, value: list[str]) -> list[str]:
        return sorted({item.strip() for item in value if item.strip()})

    @model_validator(mode="after")
    def validate_governance(self) -> ToolSpec:
        high_risk = self.risk in {
            ToolRisk.WRITE_HIGH_RISK,
            ToolRisk.HUMAN_APPROVAL_REQUIRED,
        }
        if high_risk and not self.approval_required:
            raise ValueError("high-risk tools must set approval_required=true")
        if self.risk != ToolRisk.READ_ONLY and self.retry_attempts > 1 and not self.idempotent:
            raise ValueError("non-idempotent write tools cannot enable automatic retries")
        return self

    @property
    def key(self) -> tuple[str, str]:
        return self.name, self.version

    @property
    def requires_idempotency_key(self) -> bool:
        return self.risk != ToolRisk.READ_ONLY

    def is_enabled_for(self, tenant_id: str) -> bool:
        return "*" in self.enabled_tenants or tenant_id in self.enabled_tenants


class ToolCatalog(StrictModel):
    tools: list[ToolSpec]

    @model_validator(mode="after")
    def unique_tool_versions(self) -> ToolCatalog:
        keys = [tool.key for tool in self.tools]
        if len(keys) != len(set(keys)):
            raise ValueError("tool name and version must be unique")
        return self


class ToolManifest(StrictModel):
    name: str
    version: str
    description: str
    parameters: dict[str, Any]
    required_permissions: list[str]
    risk: ToolRisk
    approval_required: bool
    timeout_seconds: float

    @classmethod
    def from_spec(cls, spec: ToolSpec) -> ToolManifest:
        return cls(
            name=spec.name,
            version=spec.version,
            description=spec.description,
            parameters=spec.input_schema,
            required_permissions=spec.required_permissions,
            risk=spec.risk,
            approval_required=spec.approval_required,
            timeout_seconds=spec.timeout_seconds,
        )


class InvocationRequest(StrictModel):
    arguments: dict[str, Any] = Field(default_factory=dict)
    version: str | None = Field(default=None, max_length=100)
    approval_id: str | None = Field(default=None, max_length=100)


class InvocationResponse(StrictModel):
    invocation_id: str = Field(default_factory=lambda: f"inv_{uuid4().hex}")
    status: InvocationStatus
    tool_name: str
    tool_version: str
    output: Any | None = None
    approval_id: str | None = None
    idempotent_replay: bool = False
    attempt_count: int = 0
    duration_ms: int = 0


class InvocationContext(StrictModel):
    tenant_id: str
    user_id: str
    permissions: frozenset[str] = Field(default_factory=frozenset)
    request_id: str
    idempotency_key: str | None = None
    trace_id: str = ""
    run_id: str = ""
    session_id: str = ""
    agent_id: str = ""
    agent_version: str = ""
    snapshot_id: str = ""
    deadline_at: datetime | None = None
    attempt_budget_remaining: int | None = Field(default=None, ge=0)


class ApprovalRecord(StrictModel):
    approval_id: str = Field(default_factory=lambda: f"approval_{uuid4().hex}")
    tenant_id: str
    user_id: str
    tool_name: str
    tool_version: str
    request_hash: str
    status: ApprovalStatus = ApprovalStatus.PENDING
    reason: str = ""
    requested_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime
    decided_by: str = ""
    decided_at: datetime | None = None


class ApprovalDecision(StrictModel):
    reason: str = Field(default="", max_length=2_000)


class AuditRecord(StrictModel):
    audit_id: str = Field(default_factory=lambda: f"audit_{uuid4().hex}")
    invocation_id: str
    request_id: str
    tenant_id: str
    user_id: str
    tool_name: str
    tool_version: str
    status: InvocationStatus
    attempt_count: int
    duration_ms: int
    arguments_sha256: str
    idempotency_key_sha256: str = ""
    error_type: str = ""
    created_at: datetime = Field(default_factory=utc_now)


class AuditPage(StrictModel):
    items: list[AuditRecord]
    count: int
