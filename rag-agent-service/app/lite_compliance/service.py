from __future__ import annotations

import re
from collections import defaultdict
from typing import Protocol

from app.lite_compliance.models import (
    DecisionStatus,
    ExternalReviewRequest,
    FeedbackInput,
    HistoryEvent,
    InternalReviewRequest,
    LiteDocument,
    RegulationClause,
    ReviewFinding,
    ReviewJob,
    ReviewMetrics,
)
from app.lite_compliance.store import LiteComplianceStore


class UncertainCaseEvaluator(Protocol):
    def evaluate(self, finding: ReviewFinding, documents: list[LiteDocument]) -> ReviewFinding: ...


def _contains(text: str, term: str) -> bool:
    return term.casefold() in text.casefold()


class LiteComplianceService:
    def __init__(
        self,
        store: LiteComplianceStore,
        uncertain_evaluator: UncertainCaseEvaluator | None = None,
    ) -> None:
        self.store = store
        self.uncertain_evaluator = uncertain_evaluator

    def register_documents(self, documents: list[LiteDocument]) -> dict:
        existing_hashes = {item.content_hash for item in self.store.documents()}
        accepted: list[LiteDocument] = []
        duplicates = 0
        for document in documents:
            if document.content_hash in existing_hashes:
                duplicates += 1
                continue
            existing_hashes.add(document.content_hash)
            accepted.append(document)
        self.store.put_documents(accepted)
        self.store.add_events(
            [
                HistoryEvent(event_type="DOCUMENT_REGISTERED", object_id=document.document_id)
                for document in accepted
            ]
        )
        return {"saved": len(accepted), "duplicates": duplicates, "total": len(documents)}

    def register_clauses(self, clauses: list[RegulationClause]) -> dict:
        for clause in clauses:
            self.store.put_clause(clause)
            self.store.add_event(
                HistoryEvent(
                    event_type="REGULATION_CLAUSE_REGISTERED", object_id=clause.clause_id
                )
            )
        return {"saved": len(clauses)}

    def external_review(self, request: ExternalReviewRequest) -> ReviewJob:
        documents = self.store.documents(request.document_ids)
        clauses = self.store.clauses(request.clause_ids)
        metrics = ReviewMetrics(documents_considered=len(documents))
        findings: list[ReviewFinding] = []

        for document in documents:
            for clause in clauses:
                metrics.total_comparisons += 1
                if (
                    clause.applicable_document_types
                    and document.document_type not in clause.applicable_document_types
                ):
                    metrics.not_applicable += 1
                    continue
                finding = self._compare_clause(document, clause)
                if finding.status == DecisionStatus.RULE_PASS:
                    metrics.rule_pass += 1
                elif finding.status == DecisionStatus.RULE_FAIL:
                    metrics.rule_fail += 1
                    findings.append(finding)
                else:
                    metrics.uncertain += 1
                    if request.allow_llm and self.uncertain_evaluator is not None:
                        finding = self.uncertain_evaluator.evaluate(finding, [document])
                        metrics.llm_calls += 1
                    findings.append(finding)

        metrics.finalize()
        job = self.store.put_job(
            ReviewJob(review_type="EXTERNAL_COMPLIANCE", metrics=metrics, findings=findings)
        )
        self.store.add_event(
            HistoryEvent(
                event_type="REVIEW_COMPLETED",
                object_id=job.job_id,
                payload={"review_type": job.review_type, **metrics.model_dump()},
            )
        )
        return job

    @staticmethod
    def _compare_clause(document: LiteDocument, clause: RegulationClause) -> ReviewFinding:
        forbidden = [term for term in clause.forbidden_terms if _contains(document.text, term)]
        matched: dict[str, str] = {}
        missing: list[str] = []
        for concept, synonyms in clause.required_concepts.items():
            hit = next((term for term in synonyms if _contains(document.text, term)), None)
            if hit:
                matched[concept] = hit
            else:
                missing.append(concept)

        common = {
            "finding_type": "EXTERNAL_CLAUSE",
            "document_ids": [document.document_id],
            "clause_id": clause.clause_id,
            "evidence": [{"matched_concepts": matched, "forbidden_terms": forbidden}],
        }
        if forbidden:
            return ReviewFinding(
                status=DecisionStatus.RULE_FAIL,
                severity="HIGH",
                reason=f"命中禁止表达：{', '.join(forbidden)}",
                **common,
            )
        if not missing:
            return ReviewFinding(
                status=DecisionStatus.RULE_PASS,
                severity="LOW",
                reason="所有必需概念均有确定性证据",
                **common,
            )
        if not matched and clause.absence_is_failure:
            return ReviewFinding(
                status=DecisionStatus.RULE_FAIL,
                severity="HIGH",
                reason=f"缺少全部必需概念：{', '.join(missing)}",
                **common,
            )
        return ReviewFinding(
            status=DecisionStatus.UNCERTAIN,
            reason=f"仅覆盖部分概念，缺少：{', '.join(missing)}",
            llm_required=True,
            **common,
        )

    def internal_review(self, request: InternalReviewRequest) -> ReviewJob:
        documents = self.store.documents(request.document_ids)
        metrics = ReviewMetrics(documents_considered=len(documents))
        findings: list[ReviewFinding] = []

        for rule in request.rules:
            pattern = re.compile(rule.value_pattern, re.IGNORECASE)
            extracted: dict[str, list[tuple[LiteDocument, str, str]]] = defaultdict(list)
            uncertain_documents: list[LiteDocument] = []
            for document in documents:
                alias = next((item for item in rule.aliases if _contains(document.text, item)), None)
                if not alias:
                    continue
                metrics.total_comparisons += 1
                match = pattern.search(document.text)
                if not match:
                    uncertain_documents.append(document)
                    continue
                value = match.groupdict().get("value") or match.group(0)
                normalized = re.sub(r"\s+", "", value).casefold()
                extracted[normalized].append((document, value, match.group(0)))

            if len(extracted) > 1:
                metrics.rule_fail += 1
                evidence = [
                    {
                        "document_id": document.document_id,
                        "filename": document.filename,
                        "value": value,
                        "quote": quote,
                    }
                    for rows in extracted.values()
                    for document, value, quote in rows
                ]
                findings.append(
                    ReviewFinding(
                        status=DecisionStatus.RULE_FAIL,
                        finding_type="INTERNAL_CONFLICT",
                        severity=rule.severity,
                        document_ids=[item["document_id"] for item in evidence],
                        rule_id=rule.rule_id,
                        reason=f"{rule.title}存在 {len(extracted)} 个不同取值",
                        evidence=evidence,
                    )
                )
            elif extracted:
                metrics.rule_pass += 1

            for document in uncertain_documents:
                metrics.uncertain += 1
                finding = ReviewFinding(
                    status=DecisionStatus.UNCERTAIN,
                    finding_type="INTERNAL_EXTRACTION_UNCERTAIN",
                    document_ids=[document.document_id],
                    rule_id=rule.rule_id,
                    reason=f"发现主题“{rule.title}”，但规则无法提取结构化取值",
                    llm_required=True,
                )
                if request.allow_llm and self.uncertain_evaluator is not None:
                    finding = self.uncertain_evaluator.evaluate(finding, [document])
                    metrics.llm_calls += 1
                findings.append(finding)

        metrics.finalize()
        job = self.store.put_job(
            ReviewJob(review_type="INTERNAL_CONSISTENCY", metrics=metrics, findings=findings)
        )
        self.store.add_event(
            HistoryEvent(
                event_type="REVIEW_COMPLETED",
                object_id=job.job_id,
                payload={"review_type": job.review_type, **metrics.model_dump()},
            )
        )
        return job

    def submit_feedback(self, feedback: FeedbackInput) -> dict:
        saved = self.store.add_feedback(feedback)
        self.store.add_event(
            HistoryEvent(
                event_type="HUMAN_FEEDBACK_SUBMITTED",
                object_id=feedback.finding_id,
                actor=feedback.reviewer,
                payload={"decision": feedback.decision, "note": feedback.note},
            )
        )
        return saved
