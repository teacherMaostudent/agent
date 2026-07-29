import pytest
from pydantic import BaseModel, Field

from app.tools.registry import ToolContext, ToolPermissionError, ToolRegistry, ToolRegistryError


class QueryArgs(BaseModel):
    query: str = Field(min_length=3)


def test_registry_enforces_schema_permission_and_audit() -> None:
    registry = ToolRegistry(default_timeout=1)
    registry.register(
        "business_query",
        "Query a business system",
        QueryArgs,
        lambda args, context: {"query": args.query, "tenant": context.tenant_id},
        {"business:read"},
    )
    denied = ToolContext("tenant-a", "user-a", frozenset(), "req-1")
    allowed = ToolContext("tenant-a", "user-a", frozenset({"business:read"}), "req-2")

    with pytest.raises(ToolPermissionError):
        registry.execute("business_query", {"query": "orders"}, denied)
    assert registry.audit_records()[-1].error_type == "ToolPermissionError"
    with pytest.raises(ToolRegistryError, match="invalid arguments"):
        registry.execute("business_query", {"query": "x"}, allowed)

    result = registry.execute("business_query", {"query": "orders"}, allowed)

    assert result == {"query": "orders", "tenant": "tenant-a"}
    assert registry.audit_records()[-1].success is True
