from __future__ import annotations

from pathlib import Path

from app.lite_compliance.models import (
    ConsistencyRule,
    ExternalReviewRequest,
    FeedbackInput,
    InternalReviewRequest,
    LiteDocument,
    RegulationClause,
)
from app.lite_compliance.service import LiteComplianceService
from app.lite_compliance.store import LiteComplianceStore


def build_service(tmp_path: Path) -> tuple[LiteComplianceService, LiteComplianceStore]:
    store = LiteComplianceStore(tmp_path / "lite.db")
    return LiteComplianceService(store), store


def test_external_review_resolves_most_cases_without_llm(tmp_path: Path) -> None:
    service, store = build_service(tmp_path)
    clause = RegulationClause(
        regulation_id="gmp-001",
        regulation_version="2026",
        title="偏差处理",
        text="偏差必须记录、调查并经质量部门批准。",
        applicable_document_types=["deviation_sop"],
        required_concepts={
            "record": ["偏差记录", "偏差报告"],
            "investigation": ["调查"],
            "approval": ["质量部门批准", "QA批准"],
        },
    )
    service.register_clauses([clause])

    documents = []
    for index in range(8_000):
        if index < 7_600:
            text = f"文件{index}：建立偏差记录，完成调查后由质量部门批准。"
        elif index < 7_800:
            text = f"文件{index}：偏差记录完成后开展调查。"  # partial => uncertain
        else:
            text = f"文件{index}：仅描述设备清洁。"  # deterministic missing
        documents.append(
            LiteDocument(
                document_id=f"doc-{index}",
                filename=f"SOP-{index}.txt",
                document_type="deviation_sop",
                text=text,
            )
        )
    imported = service.register_documents(documents)
    assert imported == {"saved": 8_000, "duplicates": 0, "total": 8_000}

    job = service.external_review(ExternalReviewRequest())

    assert job.metrics.total_comparisons == 8_000
    assert job.metrics.rule_pass == 7_600
    assert job.metrics.rule_fail == 200
    assert job.metrics.uncertain == 200
    assert job.metrics.rule_resolution_rate == 0.975
    assert job.metrics.llm_candidate_rate == 0.025
    assert job.metrics.llm_calls == 0
    store.close()


def test_internal_review_finds_numeric_conflict_without_llm(tmp_path: Path) -> None:
    service, store = build_service(tmp_path)
    service.register_documents(
        [
            LiteDocument(
                document_id="doc-a",
                filename="仓储规程.txt",
                text="物料储存温度为 2-8℃。",
            ),
            LiteDocument(
                document_id="doc-b",
                filename="物料规程.txt",
                text="该物料贮存温度为 10-20℃。",
            ),
        ]
    )

    job = service.internal_review(
        InternalReviewRequest(
            rules=[
                ConsistencyRule(
                    rule_id="storage-temperature",
                    title="储存温度",
                    aliases=["储存温度", "贮存温度"],
                    value_pattern=r"(?:储存|贮存)温度为\s*(?P<value>-?\d+\s*[-~～至]\s*-?\d+\s*℃)",
                    severity="HIGH",
                )
            ]
        )
    )

    assert job.metrics.rule_fail == 1
    assert job.metrics.uncertain == 0
    assert job.metrics.llm_calls == 0
    assert job.findings[0].finding_type == "INTERNAL_CONFLICT"
    assert len(job.findings[0].evidence) == 2
    store.close()


def test_feedback_is_visible_in_history(tmp_path: Path) -> None:
    service, store = build_service(tmp_path)
    service.submit_feedback(
        FeedbackInput(
            finding_id="finding-1",
            decision="REJECTED",
            note="该条款不适用于本产品。",
            reviewer="qa-manager",
        )
    )

    events = store.events(object_id="finding-1")
    assert len(events) == 1
    assert events[0].event_type == "HUMAN_FEEDBACK_SUBMITTED"
    assert events[0].actor == "qa-manager"
    store.close()
