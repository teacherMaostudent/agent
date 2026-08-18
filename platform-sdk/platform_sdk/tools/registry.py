"""Tool contracts and local test registry shared by Runtime and Tool clients."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import Lock
from typing import Any

from opentelemetry import trace
from pydantic import BaseModel, ValidationError


class ToolRegistryError(RuntimeError):
    pass


class ToolPermissionError(ToolRegistryError):
    pass


class ToolTimeoutError(ToolRegistryError):
    pass


@dataclass(frozen=True)
class ToolContext:
    tenant_id: str
    user_id: str
    permissions: frozenset[str] = field(default_factory=frozenset)
    request_id: str = ""
    approval_id: str = ""
    # 由 Session Runtime 在副作用前固定；重放同一 Step 必须复用它，而不是重新生成。
    tool_execution_id: str = ""
    root_task_id: str = ""
    business_operation_id: str = ""
    idempotency_key: str = ""
    trace_id: str = ""
    run_id: str = ""
    session_id: str = ""
    agent_id: str = ""
    agent_version: str = ""
    snapshot_id: str = ""
    deadline_at: str = ""
    attempt_budget_remaining: int = 0
    tool_version: str = ""


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    args_model: type[BaseModel]
    handler: Callable[[BaseModel, ToolContext], Any]
    required_permissions: frozenset[str]
    timeout_seconds: float


@dataclass(frozen=True)
class ToolAuditRecord:
    timestamp: datetime
    request_id: str
    tenant_id: str
    user_id: str
    tool_name: str
    success: bool
    duration_ms: int
    error_type: str = ""


class ToolRegistry:
    """Single enforcement point for tool schema, permission, timeout and audit."""

    def __init__(self, default_timeout: float = 20.0) -> None:
        """初始化进程内工具注册表；它只用于本地适配，不替代远程 Gateway 治理。"""
        self.default_timeout = default_timeout
        self._tools: dict[str, ToolDefinition] = {}
        self._audit: list[ToolAuditRecord] = []
        self._lock = Lock()

    def register(
        self,
        name: str,
        description: str,
        args_model: type[BaseModel],
        handler: Callable[[BaseModel, ToolContext], Any],
        required_permissions: set[str] | frozenset[str],
        timeout_seconds: float | None = None,
    ) -> None:
        """注册带参数模型、权限与超时的工具，重复名称必须失败以避免覆盖审计语义。"""
        if name in self._tools:
            raise ValueError(f"tool already registered: {name}")
        self._tools[name] = ToolDefinition(
            name=name,
            description=description,
            args_model=args_model,
            handler=handler,
            required_permissions=frozenset(required_permissions),
            timeout_seconds=timeout_seconds or self.default_timeout,
        )

    def manifests(
        self,
        permissions: frozenset[str],
        *,
        tenant_id: str = "default",
        user_id: str = "agent-runtime",
        request_id: str = "",
    ) -> list[dict[str, Any]]:
        """返回当前权限可见的工具描述；不返回不可见工具以避免目录枚举泄露。"""
        return [
            {
                "name": item.name,
                "description": item.description,
                "parameters": item.args_model.model_json_schema(),
                "required_permissions": sorted(item.required_permissions),
                "timeout_seconds": item.timeout_seconds,
            }
            for item in self._tools.values()
            if item.required_permissions.issubset(permissions)
        ]

    def execute(self, name: str, arguments: dict[str, Any], context: ToolContext) -> Any:
        """在本地执行工具并强制参数、权限、超时与审计边界。

        超时后不等待协作式任务完成，避免请求线程被卡住；生产副作用工具仍须在
        Tool Gateway 中具备幂等、审批和可取消语义。
        """
        started = datetime.now(UTC)
        error: Exception | None = None
        try:
            definition = self._tools.get(name)
            if definition is None:
                raise ToolRegistryError(f"unknown tool: {name}")
            if not definition.required_permissions.issubset(context.permissions):
                raise ToolPermissionError(f"permission denied for tool: {name}")
            try:
                validated = definition.args_model.model_validate(arguments)
            except ValidationError as exc:
                raise ToolRegistryError(f"invalid arguments for {name}: {exc}") from exc
            with trace.get_tracer(__name__).start_as_current_span(f"tool.{name}") as span:
                span.set_attribute("tool.name", name)
                span.set_attribute("tenant.id", context.tenant_id)
                pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"tool-{name}")
                future = pool.submit(definition.handler, validated, context)
                try:
                    return future.result(timeout=definition.timeout_seconds)
                except FutureTimeoutError as exc:
                    future.cancel()
                    raise ToolTimeoutError(
                        f"tool {name} exceeded {definition.timeout_seconds}s"
                    ) from exc
                finally:
                    # Do not block the request waiting for a timed-out cooperative task.
                    pool.shutdown(wait=False, cancel_futures=True)
        except Exception as exc:
            error = exc
            raise
        finally:
            duration = int((datetime.now(UTC) - started).total_seconds() * 1000)
            record = ToolAuditRecord(
                timestamp=started,
                request_id=context.request_id,
                tenant_id=context.tenant_id,
                user_id=context.user_id,
                tool_name=name,
                success=error is None,
                duration_ms=duration,
                error_type="" if error is None else type(error).__name__,
            )
            with self._lock:
                self._audit.append(record)

    def audit_records(self) -> list[ToolAuditRecord]:
        """返回审计记录的快照，调用方不能修改内部审计列表。"""
        with self._lock:
            return list(self._audit)
