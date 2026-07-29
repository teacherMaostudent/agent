import logging

from app.domain.models import (
    ChecklistItem,
    CoverageMetrics,
    CoverageStatus,
    DimensionReview,
    Evidence,
    JudgeMethod,
    ReviewResult,
    RiskLevel,
)
from app.ingestion.chunker import TextChunker
from app.knowledge.config_loader import load_grade_orders, load_regulation_floors
from app.knowledge.repository import InMemoryRepository
from app.report.markdown_renderer import MarkdownReportRenderer
from app.retrieval.hybrid_retriever import HybridRetriever
from app.review.capa_generator import CapaGenerator
from app.review.clarity_reviewer import ClarityReviewer
from app.review.data_integrity_reviewer import DataIntegrityReviewer
from app.review.field_extractor import FieldExtractor
from app.review.llm_judge import LlmJudge
from app.review.risk_scorer import RiskScorer
from app.review.standard_floor_reviewer import StandardFloorReviewer

log = logging.getLogger(__name__)

PreparedItem = tuple[ChecklistItem, list[Evidence], list[Evidence]]


class GmpReviewService:
    def __init__(
        self,
        repository: InMemoryRepository,
        retriever: HybridRetriever,
        renderer: MarkdownReportRenderer,
        judge: LlmJudge | None = None,
        semantic_retriever=None,
        llm_batch_size: int = 8,
    ) -> None:
        self.repository = repository
        self.retriever = retriever
        self.renderer = renderer
        self.semantic = semantic_retriever
        self.llm_batch_size = llm_batch_size
        self.chunker = TextChunker()
        self.extractor = FieldExtractor()
        self.risk_scorer = RiskScorer()
        self.capa = CapaGenerator()
        self.judge = judge
        self.data_integrity = DataIntegrityReviewer(self.extractor, judge=judge)
        self.clarity = ClarityReviewer(
            repository.clarity_vague_words(),
            repository.clarity_term_groups(),
        )
        self.standard_floor = StandardFloorReviewer(
            load_regulation_floors(),
            load_grade_orders(),
            judge=judge,
        )

    def review(self, document_id: str, text: str, document_type: str | None = None) -> ReviewResult:
        """执行单文档审查，并显式返回映射、判定方式和降级状态。"""
        selected, mapping = self.repository.checklist_selection(document_type)
        # 3.3 数据可靠性和 3.5 跨文档职责由专用审查器负责。
        coverage_items = [item for item in selected if item.reviewer == "coverage"]
        prepared = self._prepare_evidence(document_id, text, coverage_items)
        verdicts = self._judge_coverages(prepared, text)

        dimensions: list[DimensionReview] = []
        for item, document_evidence, regulation_evidence in prepared:
            verdict = verdicts[item.requirement_id]
            status = CoverageStatus(verdict["status"])
            missing = verdict["missing"]
            is_defect = status in {CoverageStatus.PARTIAL, CoverageStatus.MISSING}
            risk = self.risk_scorer.score(
                item.severity,
                max(len(missing), 1) if is_defect else 0,
            )
            dimensions.append(
                DimensionReview(
                    requirement_id=item.requirement_id,
                    dimension=item.dimension,
                    title=item.title,
                    passed=status == CoverageStatus.COVERED,
                    coverage_status=status,
                    judge_method=verdict["judge_method"],
                    degraded=verdict["degraded"],
                    degrade_reason=verdict.get("degrade_reason"),
                    risk_level=risk,
                    missing_points=missing,
                    reason=verdict["reason"],
                    evidence=document_evidence,
                    regulation_evidence=regulation_evidence,
                    regulation_refs=item.regulation_refs,
                    capa_suggestion=self.capa.generate(item, missing, risk),
                    need_human_review=(
                        status in {CoverageStatus.UNCERTAIN, CoverageStatus.PARTIAL}
                        or risk in {RiskLevel.HIGH, RiskLevel.CRITICAL}
                    ),
                )
            )

        data_integrity = self.data_integrity.review(
            text,
            self.repository.data_integrity_fields(),
            self.repository.data_integrity_risks(),
        )
        clarity = self.clarity.review(text)
        standard_floor = self.standard_floor.review(document_id, "", text)
        coverage = self._coverage_metrics(dimensions)

        risk_levels = [
            item.risk_level
            for item in dimensions
            if item.coverage_status in {CoverageStatus.PARTIAL, CoverageStatus.MISSING}
        ]
        if data_integrity.verdict == "存在数据可靠性风险":
            risk_levels.append(RiskLevel.HIGH)
        if standard_floor.fail_count > 0:
            risk_levels.append(RiskLevel.HIGH)
        overall = self.risk_scorer.overall(risk_levels)

        scope = f"文件类型「{document_type}」" if document_type else "通用范围"
        summary = (
            f"本次按 {scope} 核查 {len(dimensions)} 条覆盖要求，"
            f"覆盖 {coverage.covered} 条、部分覆盖 {coverage.partial} 条、"
            f"缺失 {coverage.missing} 条、待人工判断 {coverage.uncertain} 条；"
            f"数据可靠性结论：{data_integrity.verdict}；"
            f"表述清晰度：{clarity.verdict}；标准底线：{standard_floor.verdict}。"
            f"总体风险为 {overall}。"
        )
        review = ReviewResult(
            document_id=document_id,
            summary=summary,
            overall_risk=overall,
            dimensions=dimensions,
            coverage=coverage,
            mapping_status=mapping["status"],
            mapping_warning=mapping["warning"],
            data_integrity=data_integrity,
            clarity=clarity,
            standard_floor=standard_floor,
            report_markdown="",
        )
        review.report_markdown = self.renderer.render(review)
        self.repository.save_review(review)
        return review

    def _prepare_evidence(
        self,
        document_id: str,
        text: str,
        items: list[ChecklistItem],
    ) -> list[PreparedItem]:
        """企业证据和法规依据始终使用不同数据源。"""
        document_store = self.semantic.build_document_store(document_id, text) if self.semantic else None
        document_chunks = self.chunker.chunk(document_id, "enterprise_document", text, {})
        regulation_chunks = self.repository.regulation_chunks()
        prepared: list[PreparedItem] = []
        for item in items:
            query = f"{item.title} {item.description} {' '.join(item.keywords)}"
            if self.semantic:
                document_evidence = self.semantic.search_document(query, document_store, top_k=5)
            else:
                document_evidence = self.retriever.search(query, document_chunks, top_k=5)
            if self.semantic and self.semantic.has_regulation_library():
                regulation_evidence = self.semantic.search_regulations(query, top_k=3)
            else:
                regulation_evidence = self.retriever.search(query, regulation_chunks, top_k=3)
            prepared.append((item, document_evidence, regulation_evidence))
        return prepared

    def _judge_coverages(self, prepared: list[PreparedItem], text: str) -> dict[str, dict]:
        if not text.strip():
            return {
                item.requirement_id: self._uncertain_verdict(
                    item, JudgeMethod.NO_EVIDENCE, "企业文件没有可审查正文"
                )
                for item, _, _ in prepared
            }
        if self.judge is None:
            return {
                item.requirement_id: self._keyword_auxiliary(
                    item, text, "LLM 未启用，关键词仅作为人工复核线索"
                )
                for item, _, _ in prepared
            }

        verdicts: dict[str, dict] = {}
        for start in range(0, len(prepared), self.llm_batch_size):
            batch = prepared[start : start + self.llm_batch_size]
            cases = [(item, [e.text for e in evidence[:5]]) for item, evidence, _ in batch]
            try:
                if hasattr(self.judge, "judge_coverage_batch"):
                    results = self.judge.judge_coverage_batch(cases)
                else:
                    results = {
                        item.requirement_id: self.judge.judge_coverage(item, evidence)
                        for item, evidence in cases
                    }
                for item, _, _ in batch:
                    result = results.get(item.requirement_id)
                    verdicts[item.requirement_id] = (
                        self._llm_verdict(item, result)
                        if result is not None
                        else self._uncertain_verdict(
                            item, JudgeMethod.CONFIG_ERROR, "网关响应缺少该核查点"
                        )
                    )
            except Exception as exc:
                reason = f"LLM 网关调用失败: {type(exc).__name__}: {exc}"
                log.warning("coverage judge batch failed: %s", reason)
                for item, _, _ in batch:
                    verdicts[item.requirement_id] = self._keyword_auxiliary(item, text, reason)
        return verdicts

    @staticmethod
    def _llm_verdict(item: ChecklistItem, result: dict) -> dict:
        status = str(result.get("status", CoverageStatus.UNCERTAIN)).strip().upper()
        if status not in {value.value for value in CoverageStatus}:
            status = CoverageStatus.UNCERTAIN.value
        missing = [str(value) for value in result.get("missingFields", []) if value]
        if status in {CoverageStatus.PARTIAL, CoverageStatus.MISSING} and not missing:
            missing = ["整体或部分要素缺失"]
        return {
            "status": status,
            "missing": missing,
            "reason": str(result.get("reason", "")) or f"{item.title}: LLM 判定 {status}",
            "judge_method": JudgeMethod.LLM,
            "degraded": False,
            "degrade_reason": None,
        }

    def _keyword_auxiliary(self, item: ChecklistItem, text: str, reason: str) -> dict:
        """关键词只提供复核线索，不计入正式覆盖率分子或分母。"""
        keyword_hits = self.extractor.scan_keywords(text, item.keywords)
        presence = self.extractor.extract_presence(text, item.required_fields)
        missing = [field for field, found in presence.items() if not found]
        hint = "、".join(keyword_hits) if keyword_hits else "无"
        return {
            "status": CoverageStatus.UNCERTAIN,
            "missing": missing,
            "reason": f"{item.title}: 语义判定不可用，关键词命中[{hint}]，仅供人工复核。",
            "judge_method": JudgeMethod.KEYWORD_FALLBACK,
            "degraded": True,
            "degrade_reason": reason,
        }

    @staticmethod
    def _uncertain_verdict(item: ChecklistItem, method: JudgeMethod, reason: str) -> dict:
        return {
            "status": CoverageStatus.UNCERTAIN,
            "missing": [],
            "reason": f"{item.title}: {reason}，需要人工复核。",
            "judge_method": method,
            "degraded": True,
            "degrade_reason": reason,
        }

    @staticmethod
    def _coverage_metrics(dimensions: list[DimensionReview]) -> CoverageMetrics:
        counts = {status: 0 for status in CoverageStatus}
        for item in dimensions:
            counts[item.coverage_status] += 1
        denominator = (
            counts[CoverageStatus.COVERED]
            + counts[CoverageStatus.PARTIAL]
            + counts[CoverageStatus.MISSING]
        )
        rate = round(counts[CoverageStatus.COVERED] / denominator, 4) if denominator else None
        return CoverageMetrics(
            covered=counts[CoverageStatus.COVERED],
            partial=counts[CoverageStatus.PARTIAL],
            missing=counts[CoverageStatus.MISSING],
            not_applicable=counts[CoverageStatus.NOT_APPLICABLE],
            uncertain=counts[CoverageStatus.UNCERTAIN],
            denominator=denominator,
            rate=rate,
        )
