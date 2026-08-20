"""Deterministic evaluation helpers used by Governance quality gates."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from app.domain.models import (
    Finding,
    FindingStatus,
    GovernanceEvent,
    Severity,
    TenantPolicy,
    utc_now,
)


def evaluate(event: GovernanceEvent, policy: TenantPolicy) -> list[Finding]:
    """把规范化治理事件交给规则集合评估并返回
    Finding；规则只产生治理事实，不同步控制业务请求。

    Evaluate only the received event; this never calls back into the runtime.
    """
    payload = event.payload
    subject_type = str(payload.get("subject_type", event.source_service))
    subject_id = str(payload.get("subject_id", payload.get("run_id", event.event_id)))
    findings: list[Finding] = []

    def add(rule_id: str, severity: Severity, summary: str, evidence: dict[str, Any]) -> None:
        """构造绑定原事件与租户的开放发现项，避免规则命中脱离审计证据。"""
        findings.append(
            Finding(
                finding_id=f"fdg_{uuid4().hex}",
                tenant_id=event.tenant_id,
                event_id=event.event_id,
                rule_id=rule_id,
                severity=severity,
                status=FindingStatus.OPEN,
                subject_type=subject_type,
                subject_id=subject_id,
                summary=summary,
                evidence=evidence,
                created_at=utc_now(),
            )
        )

    if event.event_type == "tool.execution.completed":
        risk = payload.get("risk")
        approved = payload.get("approval_granted", False)
        if (
            policy.require_approval_for_high_risk_tools
            and risk
            in {
                "write_high_risk",
                "human_approval_required",
            }
            and not approved
        ):
            add(
                "tool.approval_required",
                Severity.CRITICAL,
                "A high-risk tool execution has no recorded approval.",
                {"tool_name": payload.get("tool_name"), "risk": risk, "approval_granted": approved},
            )

    if event.event_type == "llm.request.completed":
        model = payload.get("model")
        region = payload.get("data_region")
        if policy.allowed_models and model not in policy.allowed_models:
            add(
                "model.not_allowed",
                Severity.HIGH,
                "The model is not permitted by the tenant policy.",
                {"model": model, "allowed_models": policy.allowed_models},
            )
        if policy.allowed_data_regions and region not in policy.allowed_data_regions:
            add(
                "data_region.not_allowed",
                Severity.HIGH,
                "The model request used a disallowed data region.",
                {"data_region": region, "allowed_data_regions": policy.allowed_data_regions},
            )

    if event.event_type == "agent.run.completed":
        evidence_count = payload.get("evidence_count", 0)
        if policy.require_evidence_for_answer and (
            not isinstance(evidence_count, int) or evidence_count < 1
        ):
            add(
                "answer.evidence_required",
                Severity.MEDIUM,
                "An agent answer was completed without recorded knowledge evidence.",
                {"evidence_count": evidence_count},
            )
        _limit_finding(
            add,
            "run.cost_exceeded",
            "cost_usd",
            payload.get("cost_usd"),
            policy.max_run_cost_usd,
            "The run cost exceeded the tenant limit.",
        )
        _limit_finding(
            add,
            "run.latency_exceeded",
            "latency_ms",
            payload.get("latency_ms"),
            policy.max_run_latency_ms,
            "The run latency exceeded the tenant limit.",
        )
    return findings


def _limit_finding(
    add: Any,
    rule_id: str,
    metric: str,
    actual: Any,
    limit: int | float | None,
    summary: str,
) -> None:
    """仅在数值指标超过已配置限额时追加中风险发现，缺失数据不臆测违规。"""
    if limit is not None and isinstance(actual, (int, float)) and actual > limit:
        add(rule_id, Severity.MEDIUM, summary, {metric: actual, "limit": limit})
