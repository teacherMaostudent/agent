"""受控工具结果到 Runtime Evidence 的单向准入链路。

Tool Gateway 的职责是安全执行并校验工具协议；本模块的职责不同：它只决定一条
已完成的 Tool Observation 能否成为供本次 Agent 决策使用的证据。拒绝不会删除
原始 Observation，而会留下最小审计记录，避免失败被悄悄吞掉或不可信输出进入
后续 Prompt。
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

from jsonschema import Draft202012Validator
from platform_sdk.security import bound_untrusted

from agent_runtime_service.runtime.prompt_security import PromptSecurityGuard, PromptTrust


@dataclass(frozen=True)
class ToolEvidenceOutcome:
    """一次证据准入的结果；``record`` 始终可写入 ExecutionState。"""

    evidence: dict[str, Any] | None
    record: dict[str, Any]


class ToolEvidencePipeline:
    """执行 Observation → Evidence 的确定性处理，不调用模型、不改变工具副作用。

    该流水线刻意只产生 ``ephemeral`` 证据。长期知识沉淀仍走 Artifact 审批与
    Ingestion/RAG 的独立链路，不能借一次工具调用绕过数据生命周期和人工审批。
    """

    _GENERIC_RESULT_SCHEMA: ClassVar[dict[str, list[str]]] = {
        "type": ["object", "array", "string", "number", "boolean", "null"],
    }

    def __init__(self, prompt_security: PromptSecurityGuard | None = None) -> None:
        self._prompt_security = prompt_security or PromptSecurityGuard()

    def process(
        self,
        *,
        observation: dict[str, Any],
        binding: dict[str, Any],
        tenant_id: str,
        user_id: str,
        permissions: list[str],
        run_id: str,
        step_id: str,
        observed_at: datetime | None = None,
    ) -> ToolEvidenceOutcome:
        """按固定顺序解析、验证并投影一条成功工具结果。

        任何一个阶段失败都会返回 ``evidence=None``。调用方仍把拒绝记录保留在
        ``tool_evidence``，而不是将失败结果伪装成空 Evidence。
        """
        now = observed_at or datetime.now(UTC)
        tool_name = str(observation.get("tool", ""))
        base = {
            "tool_name": tool_name,
            "tool_version": str(binding.get("version", "")),
            "run_id": run_id,
            "step_id": step_id,
            "tenant_id": tenant_id,
            "processed_at": now.isoformat(),
            "store": "runtime_execution_state",
            "persistence": "ephemeral",
        }

        # Tool 失败、审批拒绝和子 Agent 结果是 Observation，不可被误标为工具证据。
        if observation.get("type") != "tool" or not observation.get("success"):
            return self._reject(base, "OBSERVATION_NOT_SUCCESSFUL")
        if not tool_name or tool_name != str(binding.get("tool_name", "")):
            return self._reject(base, "PUBLISHED_BINDING_MISMATCH")

        try:
            parsed, canonical = self._parse(observation.get("result"))
        except ValueError as exc:
            return self._reject(base, f"PARSER_REJECTED:{exc}")

        schema = binding.get("output_schema") or binding.get("config", {}).get("output_schema")
        try:
            Draft202012Validator(schema or self._GENERIC_RESULT_SCHEMA).validate(parsed)
        except Exception as exc:
            return self._reject(base, f"SCHEMA_INVALID:{type(exc).__name__}")

        acl_reason = self._validate_security_acl_freshness(
            binding=binding,
            tenant_id=tenant_id,
            user_id=user_id,
            permissions=permissions,
            result=parsed,
            now=now,
        )
        if acl_reason:
            return self._reject(base, acl_reason)

        findings = self._prompt_security.inspect(
            parsed,
            trust=PromptTrust.TOOL,
            source_id=f"tool:{tool_name}",
        )
        if findings:
            return self._reject(base, "SECURITY_PROMPT_INJECTION", findings=[item.code for item in findings])

        text = self._extract_text(parsed)
        if not text:
            return self._reject(base, "EVIDENCE_EMPTY")
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        evidence_id = f"tev_{hashlib.sha256(f'{run_id}:{step_id}:{tool_name}:{digest}'.encode()).hexdigest()[:24]}"
        evidence = {
            "evidence_id": evidence_id,
            "source_id": f"tool://{tool_name}/{binding.get('version', '')}/{digest[:16]}",
            "source_type": "tool_observation",
            "text": text,
            "score": 1.0,
            "metadata": {
                "tool_name": tool_name,
                "tool_version": str(binding.get("version", "")),
                "tenant_id": tenant_id,
                "user_id": user_id,
                "run_id": run_id,
                "step_id": step_id,
                "observed_at": now.isoformat(),
                "fresh_until": self._fresh_until(binding, now),
                "content_sha256": digest,
                "verification": "runtime-tool-evidence/v1",
                "persistence": "ephemeral",
            },
        }
        return ToolEvidenceOutcome(
            evidence=evidence,
            record={
                **base,
                "status": "STORED",
                "evidence_id": evidence_id,
                "content_sha256": digest,
                "verification": "PASSED",
            },
        )

    @staticmethod
    def _parse(value: Any) -> tuple[Any, str]:
        """限制性 JSON 解析，拒绝不可重放的 NaN、无限值和非字符串键。"""
        bounded = bound_untrusted(value, 12_000)
        try:
            canonical = json.dumps(bounded, ensure_ascii=False, sort_keys=True, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("non_canonical_result") from exc
        parsed = json.loads(canonical)
        if ToolEvidencePipeline._contains_non_finite(parsed):
            raise ValueError("non_finite_number")
        return parsed, canonical

    @staticmethod
    def _contains_non_finite(value: Any) -> bool:
        if isinstance(value, float):
            return not math.isfinite(value)
        if isinstance(value, dict):
            return any(ToolEvidencePipeline._contains_non_finite(item) for item in value.values())
        if isinstance(value, list):
            return any(ToolEvidencePipeline._contains_non_finite(item) for item in value)
        return False

    @staticmethod
    def _validate_security_acl_freshness(
        *, binding: dict[str, Any], tenant_id: str, user_id: str, permissions: list[str], result: Any, now: datetime
    ) -> str:
        """复核发布绑定与观察时的身份事实；不信任结果自己声明的权限。"""
        if not tenant_id or not user_id:
            return "SECURITY_IDENTITY_MISSING"
        required = {str(item) for item in binding.get("required_permissions", [])}
        if not required.issubset(set(permissions)):
            return "ACL_PERMISSION_MISMATCH"
        policy = binding.get("config", {}).get("evidence", {})
        if policy and not isinstance(policy, dict):
            return "EVIDENCE_POLICY_INVALID"
        if isinstance(policy, dict) and policy.get("enabled") is False:
            return "EVIDENCE_DISABLED_BY_POLICY"
        max_age = int(policy.get("max_age_seconds", 300)) if isinstance(policy, dict) else 300
        if max_age <= 0:
            return "FRESHNESS_POLICY_INVALID"
        source_time = result.get("observed_at") if isinstance(result, dict) else None
        if source_time:
            try:
                parsed = datetime.fromisoformat(str(source_time).replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    return "FRESHNESS_TIMESTAMP_NOT_TIMEZONE_AWARE"
                if now - parsed.astimezone(UTC) > timedelta(seconds=max_age):
                    return "FRESHNESS_EXPIRED"
            except ValueError:
                return "FRESHNESS_TIMESTAMP_INVALID"
        return ""

    @staticmethod
    def _extract_text(value: Any) -> str:
        """提取供模型阅读的受限文本；保留结构但不让其成为可执行指令。"""
        if isinstance(value, str):
            return str(bound_untrusted(value, 8_000)).strip()
        if isinstance(value, dict) and isinstance(value.get("text"), str):
            return str(bound_untrusted(value["text"], 8_000)).strip()
        rendered = json.dumps(bound_untrusted(value, 8_000), ensure_ascii=False, sort_keys=True)
        return rendered.strip() if rendered not in {"{}", "[]", "null", '""'} else ""

    @staticmethod
    def _fresh_until(binding: dict[str, Any], observed_at: datetime) -> str:
        policy = binding.get("config", {}).get("evidence", {})
        max_age = int(policy.get("max_age_seconds", 300)) if isinstance(policy, dict) else 300
        return (observed_at + timedelta(seconds=max_age)).isoformat()

    @staticmethod
    def _reject(base: dict[str, Any], reason: str, *, findings: list[str] | None = None) -> ToolEvidenceOutcome:
        return ToolEvidenceOutcome(
            evidence=None,
            record={
                **base,
                "status": "REJECTED",
                "reason": reason,
                "findings": findings or [],
            },
        )
