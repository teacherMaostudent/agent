
import pytest
from platform_sdk.contracts.execution import ExecutionContext
from platform_sdk.contracts.skills import (
    CapabilityProviderKind,
    CapabilityRoutingPolicy,
)

from agent_runtime_service.runtime.capability_dispatcher import (
    CapabilityDispatchError,
    GovernedCapabilityDispatcher,
)


def _context():
    return ExecutionContext.create(
        request_id="req",
        trace_id="trace",
        session_id="session",
        tenant_id="tenant",
        user_id="user",
        agent_id="agent",
        agent_version="1",
        snapshot_id="snapshot",
        deadline_seconds=30,
        attempt_budget=3,
    )


def _provider(provider_id, kind, *, health="available"):
    payload = {
        "provider_id": provider_id,
        "kind": kind,
        "capabilities": [{"capability_id": "LEGAL_REVIEW"}],
        "version": "1.0.0",
        "health_status": health,
        "input_schema": {"type": "object", "required": ["task"]},
        "output_schema": {"type": "object", "required": ["decision"]},
    }
    if kind in {"skill", "workflow"}:
        payload["artifact_digest"] = "a" * 64
    return payload


def test_dispatcher_uses_capability_specific_fallback_and_validates_output():
    dispatcher = GovernedCapabilityDispatcher(
        [_provider("skill", "skill", health="unavailable"), _provider("legal", "agent")],
        [
            CapabilityRoutingPolicy(
                capability_id="LEGAL_REVIEW", provider_order=["skill", "legal"]
            )
        ],
        {CapabilityProviderKind.AGENT: lambda provider, payload, context: {"decision": "ok"}},
    )
    result = dispatcher.dispatch("LEGAL_REVIEW", {"task": "review"}, _context())
    assert result.provider_id == "legal"
    assert result.provider_kind == CapabilityProviderKind.AGENT


def test_dispatcher_rejects_provider_output_schema_drift():
    dispatcher = GovernedCapabilityDispatcher(
        [_provider("legal", "agent")],
        [],
        {CapabilityProviderKind.AGENT: lambda provider, payload, context: {"wrong": True}},
    )
    with pytest.raises(CapabilityDispatchError, match="output violates schema"):
        dispatcher.dispatch("LEGAL_REVIEW", {"task": "review"}, _context())


def test_dispatcher_falls_back_only_when_failed_provider_declares_safe_replay():
    primary = _provider("primary", "agent")
    primary["fallback_safe"] = True
    dispatcher = GovernedCapabilityDispatcher(
        [primary, _provider("backup", "agent")],
        [
            CapabilityRoutingPolicy(
                capability_id="LEGAL_REVIEW", provider_order=["primary", "backup"]
            )
        ],
        {
            CapabilityProviderKind.AGENT: lambda provider, payload, context: (
                (_ for _ in ()).throw(RuntimeError("temporary"))
                if provider.provider_id == "primary"
                else {"decision": "backup"}
            )
        },
    )
    result = dispatcher.dispatch("LEGAL_REVIEW", {"task": "review"}, _context())
    assert result.provider_id == "backup"
