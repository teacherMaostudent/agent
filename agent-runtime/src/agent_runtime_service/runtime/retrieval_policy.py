"""Retrieval profiles and the runtime policy resolver.

Profiles are templates.  Only the resolved policy is placed in the run state;
the Graph never trusts the classifier's profile without this resolver.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class RetrievalProfile(StrEnum):
    NO_RAG = "NO_RAG"
    FAST = "FAST"
    STANDARD = "STANDARD"
    STRICT_EVIDENCE = "STRICT_EVIDENCE"
    DEEP_RESEARCH = "DEEP_RESEARCH"


class ProfileDecision(BaseModel):
    requested_profile: RetrievalProfile
    confidence: float = Field(ge=0, le=1)
    reason_codes: list[str] = Field(default_factory=list)


class EffectiveRetrievalPolicy(BaseModel):
    profile: RetrievalProfile
    # Profile revision is part of retrieval lineage. Runtime selects a named
    # profile while RAG executes this immutable revision and rejects drift.
    profile_revision: str = "retrieval-profile/v1"
    retrieval_required: bool
    max_rounds: int = Field(ge=0)
    candidate_top_k: int = Field(ge=0)
    evidence_top_k: int = Field(ge=0)
    hybrid_search: bool = True
    rerank_enabled: bool = False
    minimum_evidence_count: int = Field(default=0, ge=0)
    citation_required: bool = False
    allow_answer_without_evidence: bool = True
    decision: str = "ACCEPTED"
    reason: str = ""


_TEMPLATES: dict[RetrievalProfile, dict[str, Any]] = {
    RetrievalProfile.NO_RAG: {
        "retrieval_required": False,
        "max_rounds": 0,
        "candidate_top_k": 0,
        "evidence_top_k": 0,
        "hybrid_search": False,
        "rerank_enabled": False,
        "minimum_evidence_count": 0,
        "citation_required": False,
        "allow_answer_without_evidence": True,
    },
    RetrievalProfile.FAST: {
        "retrieval_required": False,
        "max_rounds": 1,
        "candidate_top_k": 12,
        "evidence_top_k": 3,
        "hybrid_search": False,
        "rerank_enabled": False,
        "minimum_evidence_count": 0,
        "citation_required": False,
        "allow_answer_without_evidence": True,
    },
    RetrievalProfile.STANDARD: {
        "retrieval_required": False,
        "max_rounds": 2,
        "candidate_top_k": 30,
        "evidence_top_k": 5,
        "hybrid_search": True,
        "rerank_enabled": True,
        "minimum_evidence_count": 1,
        "citation_required": False,
        "allow_answer_without_evidence": True,
    },
    RetrievalProfile.STRICT_EVIDENCE: {
        "retrieval_required": True,
        "max_rounds": 3,
        "candidate_top_k": 50,
        "evidence_top_k": 5,
        "hybrid_search": True,
        "rerank_enabled": True,
        "minimum_evidence_count": 1,
        "citation_required": True,
        "allow_answer_without_evidence": False,
    },
    RetrievalProfile.DEEP_RESEARCH: {
        "retrieval_required": True,
        "max_rounds": 6,
        "candidate_top_k": 80,
        "evidence_top_k": 10,
        "hybrid_search": True,
        "rerank_enabled": True,
        "minimum_evidence_count": 2,
        "citation_required": True,
        "allow_answer_without_evidence": False,
    },
}


def infer_profile(task: str, intent: str, metadata: dict[str, Any]) -> ProfileDecision:
    """从请求信号推断候选检索档位，结果仍须被 ``resolve_profile`` 收紧。

    显式请求可表达用户意图，但不能直接扩大能力；非法枚举退回标准档位并保留原因，
    使治理系统能够发现调用方配置错误。
    """
    explicit = metadata.get("retrieval_profile") or metadata.get("retrievalProfile")
    if explicit:
        try:
            return ProfileDecision(
                requested_profile=RetrievalProfile(str(explicit).upper()),
                confidence=1.0,
                reason_codes=["EXPLICIT_REQUEST"],
            )
        except ValueError:
            return ProfileDecision(
                requested_profile=RetrievalProfile.STANDARD,
                confidence=0.0,
                reason_codes=["INVALID_PROFILE_DEFAULTED"],
            )
    lowered = task.lower()
    if any(
        word in lowered for word in ("no search", "without retrieval", "不要检索", "仅根据上文")
    ):
        return ProfileDecision(
            requested_profile=RetrievalProfile.NO_RAG,
            confidence=0.98,
            reason_codes=["NO_RETRIEVAL_REQUEST"],
        )
    if (
        any(
            word in lowered
            for word in ("must cite", "citation", "no evidence", "必须引用", "没有依据")
        )
        or intent == "compliance_review"
    ):
        return ProfileDecision(
            requested_profile=RetrievalProfile.STRICT_EVIDENCE,
            confidence=0.9,
            reason_codes=["EVIDENCE_REQUIRED"],
        )
    if any(word in lowered for word in ("deep research", "cross-check", "全面调研", "交叉验证")):
        return ProfileDecision(
            requested_profile=RetrievalProfile.DEEP_RESEARCH,
            confidence=0.9,
            reason_codes=["DEEP_RESEARCH_REQUEST"],
        )
    if intent in {"knowledge_query", "general_question"}:
        return ProfileDecision(
            requested_profile=RetrievalProfile.STANDARD,
            confidence=0.75,
            reason_codes=["KNOWLEDGE_INTENT_DEFAULT"],
        )
    return ProfileDecision(
        requested_profile=RetrievalProfile.FAST,
        confidence=0.7,
        reason_codes=["FAST_DEFAULT"],
    )


def resolve_profile(
    decision: ProfileDecision,
    *,
    snapshot: dict[str, Any] | None,
    budget: dict[str, Any] | None,
    metadata: dict[str, Any] | None = None,
) -> EffectiveRetrievalPolicy:
    """将候选档位与发布快照、剩余预算求交集，形成唯一可执行策略。

    快照白名单和硬轮次上限优先于分类器；没有可用检索轮次时明确降级为 NO_RAG，
    由 Graph 根据 ``retrieval_required`` 决定失败关闭还是允许回答。
    """
    metadata = metadata or {}
    spec = (snapshot or {}).get("spec", {}) if isinstance(snapshot, dict) else {}
    configured = spec.get("retrieval_policy", {}) if isinstance(spec, dict) else {}
    allowed = configured.get("allowed_profiles") or metadata.get("allowed_profiles")
    allowed_profiles = (
        {RetrievalProfile(str(item).upper()) for item in allowed}
        if allowed
        else set(RetrievalProfile)
    )
    selected = decision.requested_profile
    reason = "requested profile is allowed"
    outcome = "ACCEPTED"
    if selected not in allowed_profiles:
        fallback = configured.get("default_profile", RetrievalProfile.STANDARD)
        selected = RetrievalProfile(str(fallback).upper())
        if selected not in allowed_profiles:
            selected = max(
                allowed_profiles or {RetrievalProfile.STANDARD},
                key=lambda item: list(RetrievalProfile).index(item),
            )
        outcome, reason = "DOWNGRADED", "PROFILE_NOT_ALLOWED"
    template = dict(_TEMPLATES[selected])
    hard_rounds = int(configured.get("hard_max_rounds", 100))
    budget_rounds = int((budget or {}).get("max_retrieval_rounds", hard_rounds))
    template["max_rounds"] = min(template["max_rounds"], hard_rounds, budget_rounds)
    if selected != RetrievalProfile.NO_RAG and template["max_rounds"] < 1:
        selected = RetrievalProfile.NO_RAG
        template = dict(_TEMPLATES[selected])
        outcome, reason = "DOWNGRADED", "RETRIEVAL_ROUND_BUDGET_EXHAUSTED"
    overrides = configured.get("overrides", {}).get(selected.value, {})
    for key in (
        "candidate_top_k",
        "evidence_top_k",
        "rerank_enabled",
        "citation_required",
        "allow_answer_without_evidence",
    ):
        if key in overrides:
            template[key] = overrides[key]
    return EffectiveRetrievalPolicy(
        profile=selected,
        profile_revision=str(configured.get("profile_revision", "retrieval-profile/v1")),
        decision=outcome,
        reason=reason,
        **template,
    )
