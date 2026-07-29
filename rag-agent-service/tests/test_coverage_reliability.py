import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.knowledge.repository import InMemoryRepository
from app.report.markdown_renderer import MarkdownReportRenderer
from app.retrieval.hybrid_retriever import HybridRetriever
from app.review.gmp_reviewer import GmpReviewService


def _service(judge=None, batch_size: int = 8) -> GmpReviewService:
    return GmpReviewService(
        repository=InMemoryRepository(),
        retriever=HybridRetriever(bm25_weight=0.55, vector_weight=0.45, embedding_dim=384),
        renderer=MarkdownReportRenderer(),
        judge=judge,
        llm_batch_size=batch_size,
    )


class _BatchJudge:
    def __init__(self) -> None:
        self.batch_calls = 0

    def judge_coverage_batch(self, cases):
        self.batch_calls += 1
        return {
            item.requirement_id: {
                "status": "COVERED",
                "evidence": evidence[0] if evidence else "",
                "missingFields": [],
                "reason": "批量语义判定已覆盖",
            }
            for item, evidence in cases
        }

    def judge_data_integrity(self, content, fields, risks):
        return {
            "fields": [{"id": item.id, "present": "PRESENT"} for item in fields],
            "risks": [{"id": item.id, "verdict": "OK"} for item in risks],
        }

    def extract_assertions(self, filename, text, attribute_hints):
        return {"assertions": []}


def test_llm_configuration_requires_model() -> None:
    with pytest.raises(ValidationError):
        Settings(llm_enabled=True, llm_model="")


def test_offline_keyword_result_is_explicitly_uncertain() -> None:
    review = _service().review(
        "doc-offline",
        "本文件包含文件编号、版本号、起草人、审核人、批准人和生效日期。",
        "场地管理文件管理",
    )
    assert review.coverage.rate is None
    assert review.coverage.uncertain == len(review.dimensions)
    assert all(item.coverage_status == "UNCERTAIN" for item in review.dimensions)
    assert all(item.judge_method == "KEYWORD_FALLBACK" for item in review.dimensions)
    assert all(item.degraded for item in review.dimensions)


def test_enterprise_and_regulation_evidence_are_separated() -> None:
    review = _service().review("doc-evidence", "文件管理、版本、批准和定期评审。", "场地管理文件管理")
    assert review.dimensions
    assert all(e.source_type == "enterprise_document" for d in review.dimensions for e in d.evidence)
    assert all(e.source_type == "regulation" for d in review.dimensions for e in d.regulation_evidence)


def test_dedicated_reviewers_are_not_repeated_in_coverage_loop() -> None:
    repository = InMemoryRepository()
    assert repository.checklist["REQ-REC-001"].reviewer == "data_integrity"
    assert repository.checklist["REQ-DI-001"].reviewer == "data_integrity"
    assert repository.checklist["REQ-ROLE-001"].reviewer == "cross_document"
    review = _service().review("doc-change", "任意正文", "变更管理")
    assert {item.requirement_id for item in review.dimensions} == {"REQ-CHANGE-001"}


def test_batch_judge_reduces_calls_and_produces_coverage_rate() -> None:
    judge = _BatchJudge()
    review = _service(judge=judge, batch_size=1).review(
        "doc-batch", "任意可审查正文", "场地管理文件管理"
    )
    assert judge.batch_calls == 2
    assert review.coverage.covered == 2
    assert review.coverage.rate == 1.0
    assert all(item.judge_method == "LLM" for item in review.dimensions)


def test_unmapped_type_returns_visible_warning() -> None:
    review = _service().review("doc-map", "任意正文", "稳定性试验管理")
    assert review.mapping_status == "DEFAULT_ONLY"
    assert review.mapping_warning
    assert "不代表完整条款覆盖率" in review.mapping_warning


def test_mapping_diagnostics_exposes_missing_types() -> None:
    diagnostics = InMemoryRepository().mapping_diagnostics()
    assert diagnostics["total_document_types"] == 66
    assert diagnostics["mapped_document_types"] == 25
    assert diagnostics["unmapped_document_types"] == 41
