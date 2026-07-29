"""阶段 B：大模型判定路径测试。

用假 judge(不联网)验证两点：
1. judge 返回结果被正确采纳(LLM 路径生效);
2. judge 抛异常时回退到关键词基线,服务不崩(容错)。
"""

from app.knowledge.repository import InMemoryRepository
from app.report.markdown_renderer import MarkdownReportRenderer
from app.retrieval.hybrid_retriever import HybridRetriever
from app.review.gmp_reviewer import GmpReviewService


def _make_service(judge) -> GmpReviewService:
    return GmpReviewService(
        repository=InMemoryRepository(),
        retriever=HybridRetriever(bm25_weight=0.55, vector_weight=0.45, embedding_dim=384),
        renderer=MarkdownReportRenderer(),
        judge=judge,
    )


class _FakeJudge:
    """所有覆盖率都判 COVERED，数据可靠性全部 OK/PRESENT。"""

    def judge_coverage(self, item, evidence):
        return {"status": "COVERED", "evidence": "文件已规定", "missingFields": [], "reason": "模型判定已覆盖"}

    def judge_data_integrity(self, content, fields, risks):
        return {
            "fields": [{"id": f.id, "present": "PRESENT", "evidence": "", "comment": ""} for f in fields],
            "risks": [{"id": r.id, "verdict": "OK", "evidence": "", "comment": ""} for r in risks],
        }


class _BrokenJudge:
    """模拟网关不通：任何调用都抛异常，应触发关键词回退。"""

    def judge_coverage(self, item, evidence):
        raise RuntimeError("gateway unreachable")

    def judge_data_integrity(self, content, fields, risks):
        raise RuntimeError("gateway unreachable")


def test_llm_path_marks_all_covered() -> None:
    service = _make_service(_FakeJudge())
    review = service.review(document_id="doc1", text="任意内容", document_type="场地管理文件管理")
    assert all(d.passed for d in review.dimensions)
    assert review.data_integrity.verdict == "未发现明显数据可靠性问题"
    assert "模型判定已覆盖" in review.dimensions[0].reason


def test_broken_judge_falls_back_to_keyword() -> None:
    """judge 抛异常时应回退关键词基线，红旗词仍能被抓到。"""
    service = _make_service(_BrokenJudge())
    review = service.review(
        document_id="doc2",
        text="操作记录可在当班结束后统一补录，凭记忆填写即可。",
    )
    # 回退成功 → 服务正常返回，且关键词基线抓到"补录"红旗词。
    assert review.status == "COMPLETED"
    assert review.data_integrity.verdict == "存在数据可靠性风险"
