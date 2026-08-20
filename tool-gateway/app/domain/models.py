from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utc_now() -> datetime:
    """生成带 UTC 时区的默认时间，确保审批、执行和审计记录可跨节点排序。
    无效配置延迟到执行期失败。 无效配置延迟到执行期失败。

    Generate UTC timestamps so approvals and audit expiry are comparable across regions.
    """
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
    allow_private_networks: bool = False

    @field_validator("allowed_hosts")
    @classmethod
    def normalize_hosts(cls, value: list[str]) -> list[str]:
        """规范化并去重允许主机列表；空值、通配根域和重复项在配置加载期处理。
        化和不变量校验，避免无效配置延迟到执行期失败。
        化和不变量校验，避免无效配置延迟到执行期失败。

        Canonicalise allow-listed hosts before SSRF validation compares them.
        """
        return sorted({item.strip().lower() for item in value if item.strip()})


class McpTransport(StrictModel):
    kind: Literal["mcp_streamable_http"]
    server_url: str = Field(min_length=8, max_length=2_000)
    remote_tool_name: str = Field(min_length=1, max_length=200)
    static_headers: dict[str, str] = Field(default_factory=dict)
    auth_header: str | None = Field(default=None, max_length=100)
    auth_env: str | None = Field(default=None, max_length=200)
    allowed_hosts: list[str] = Field(min_length=1)
    allow_private_networks: bool = False

    @field_validator("allowed_hosts")
    @classmethod
    def normalize_hosts(cls, value: list[str]) -> list[str]:
        """规范化并去重允许主机列表；空值、通配根域和重复项在配置加载期处理。
        化和不变量校验，避免无效配置延迟到执行期失败。
        化和不变量校验，避免无效配置延迟到执行期失败。

        Canonicalise MCP server host allow-lists using the same SSRF-safe rule.
        """
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
        """保持工具权限与租户列表的声明顺序去重，避免同一约束被重复执行。
        不变量校验，避免无效配置延迟到执行期失败。
        不变量校验，避免无效配置延迟到执行期失败。

        Remove blank and duplicate permissions or tenant bindings at catalog load time.
        """
        return sorted({item.strip() for item in value if item.strip()})

    @model_validator(mode="after")
    def validate_governance(self) -> ToolSpec:
        """校验‘validate_governance’资源的领域约束，在对象进入应用层前
        完成规范化和不变量校验，避免无效配置延迟到执行期失败。
        完成规范化和不变量校验，避免无效配置延迟到执行期失败。
        完成规范化和不变量校验，避免无效配置延迟到执行期失败。

        Forbid unsafe retry and approval combinations before a tool reaches Runtime.
        """
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
        """返回工具名称和版本组成的不可变目录键，禁止仅按名称覆盖另一版本。
        配置延迟到执行期失败。 配置延迟到执行期失败。

        Return the immutable catalog identity used for version-safe lookup.
        """
        return self.name, self.version

    @property
    def requires_idempotency_key(self) -> bool:
        """处理幂等执行记录的领域约束，在对象进入应用层前完成规范化和不变量校验，避免无效配
        置延迟到执行期失败。

        Require replay protection for every operation that can change business state.
        """
        return self.risk != ToolRisk.READ_ONLY

    def is_enabled_for(self, tenant_id: str) -> bool:
        """处理‘is_enabled_for’资源的领域约束，在对象进入应用层前完成规范化
        和不变量校验，避免无效配置延迟到执行期失败。
        和不变量校验，避免无效配置延迟到执行期失败。
        和不变量校验，避免无效配置延迟到执行期失败。

        Check the tenant allow-list before exposing a tool or invoking its adapter.
        """
        return "*" in self.enabled_tenants or tenant_id in self.enabled_tenants


class ToolCatalog(StrictModel):
    tools: list[ToolSpec]

    @model_validator(mode="after")
    def unique_tool_versions(self) -> ToolCatalog:
        """处理工具目录项的领域约束，在对象进入应用层前完成规范化和不变量校验，避免无效配置
        延迟到执行期失败。

        Reject duplicate logical versions so release selection stays unambiguous.
        """
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
        """从内部 ToolSpec 生成不含凭据和端点秘密的对外
        ToolManifest。 验，避免无效配置延迟到执行期失败。
        验，避免无效配置延迟到执行期失败。

        Project an executable catalog entry into the safe manifest visible to Runtime.
        """
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
    authorization: dict[str, Any] | None = None


class ToolExecutionState(StrictModel):
    """供 Runtime 恢复查询的只读幂等执行状态, 绝不重新触发业务副作用。"""

    tool_name: str
    idempotency_key_sha256: str
    status: Literal["NOT_FOUND", "IN_PROGRESS", "COMPLETED"]
    response: InvocationResponse | None = None


class InvocationContext(StrictModel):
    tenant_id: str
    user_id: str
    permissions: frozenset[str] = Field(default_factory=frozenset)
    request_id: str
    idempotency_key: str | None = None
    tool_execution_id: str = ""
    root_task_id: str = ""
    business_operation_id: str = ""
    operation_id: str = ""
    step_id: str = ""
    plan_id: str = ""
    plan_admission_id: str = ""
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
