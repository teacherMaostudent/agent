"""数据可靠性(ALCOA+)核查(设计文档 3.3)。

阶段 A 为关键词基线：字段用 keywords 命中判 present；风险项用 red_flags/expect
命中判 verdict。阶段 B 接入 llm-gateway：judge 非 None 时用一次性大模型判定
(移植自 Java DataIntegrityService 的 systemPrompt)，调用失败回退关键词基线。
"""

import logging

from app.domain.models import (
    AlcoaRisk,
    AlcoaRiskResult,
    DataIntegrityFieldCheck,
    DataIntegrityReport,
    FieldCheckResult,
    RiskLevel,
)
from app.review.field_extractor import FieldExtractor
from app.review.llm_judge import LlmJudge

log = logging.getLogger(__name__)


class DataIntegrityReviewer:
    def __init__(self, extractor: FieldExtractor | None = None, judge: LlmJudge | None = None) -> None:
        self.extractor = extractor or FieldExtractor()
        self.judge = judge

    def review(
        self,
        text: str,
        field_checks: list[DataIntegrityFieldCheck],
        risks: list[AlcoaRisk],
    ) -> DataIntegrityReport:
        field_results, risk_results, method, degraded, degrade_reason = self._judge(text, field_checks, risks)
        return self._aggregate(field_results, risk_results, method, degraded, degrade_reason)

    def _judge(
        self,
        text: str,
        field_checks: list[DataIntegrityFieldCheck],
        risks: list[AlcoaRisk],
    ) -> tuple[list[FieldCheckResult], list[AlcoaRiskResult], str, bool, str | None]:
        """优先大模型判定；降级时把原因写入报告。"""
        if self.judge is not None:
            try:
                out = self.judge.judge_data_integrity(text, field_checks, risks)
                by_field = {n.get("id"): n for n in out.get("fields", [])}
                by_risk = {n.get("id"): n for n in out.get("risks", [])}
                fields = [self._field_from_llm(f, by_field.get(f.id)) for f in field_checks]
                risk_res = [self._risk_from_llm(r, by_risk.get(r.id)) for r in risks]
                return fields, risk_res, "LLM", False, None
            except Exception as exc:
                log.warning("data-integrity judge failed err=%s, fallback to keyword", exc)
                reason = f"LLM 网关调用失败: {type(exc).__name__}: {exc}"
        else:
            reason = "LLM 未启用，关键词结果仅供人工复核"
        return (
            [self._field_from_keyword(text, f) for f in field_checks],
            [self._risk_from_keyword(text, r) for r in risks],
            "KEYWORD_FALLBACK",
            True,
            reason,
        )

    # --- 大模型判定结果解析 ---

    @staticmethod
    def _field_from_llm(f: DataIntegrityFieldCheck, node: dict | None) -> FieldCheckResult:
        present = str((node or {}).get("present", "UNKNOWN")).strip().upper()
        if present not in {"PRESENT", "MISSING"}:
            present = "UNKNOWN"
        return FieldCheckResult(
            id=f.id, field=f.field, present=present, severity=f.severity,
            evidence=str((node or {}).get("evidence", "")),
            comment=str((node or {}).get("comment", "")),
        )

    @staticmethod
    def _risk_from_llm(r: AlcoaRisk, node: dict | None) -> AlcoaRiskResult:
        verdict = str((node or {}).get("verdict", "UNCLEAR")).strip().upper()
        if verdict not in {"OK", "RISK", "UNCLEAR"}:
            verdict = "UNCLEAR"
        return AlcoaRiskResult(
            id=r.id, principle=r.principle, risk=r.risk, verdict=verdict, severity=r.severity,
            evidence=str((node or {}).get("evidence", "")),
            comment=str((node or {}).get("comment", "")),
        )

    # --- 关键词基线 ---

    def _field_from_keyword(self, text: str, f: DataIntegrityFieldCheck) -> FieldCheckResult:
        hits = self.extractor.scan_keywords(text, f.keywords)
        present = "PRESENT" if hits else "MISSING"
        comment = f"命中关键词: {', '.join(hits)}" if hits else "未在文件中找到该字段相关表述"
        return FieldCheckResult(
            id=f.id, field=f.field, present=present, severity=f.severity, evidence="", comment=comment,
        )

    def _risk_from_keyword(self, text: str, r: AlcoaRisk) -> AlcoaRiskResult:
        hit_flags, hit_expect = self.extractor.scan_risk(text, r.red_flags, r.expect)
        # 命中红旗词 → RISK；只命中期望词 → OK；都没命中 → UNCLEAR(文件未提及)。
        if hit_flags:
            verdict, comment = "RISK", f"发现风险表述: {', '.join(hit_flags)}"
        elif hit_expect:
            verdict, comment = "OK", f"发现合规做法: {', '.join(hit_expect)}"
        else:
            verdict, comment = "UNCLEAR", "文件未明确提及该点，无法判断"
        return AlcoaRiskResult(
            id=r.id, principle=r.principle, risk=r.risk, verdict=verdict, severity=r.severity,
            evidence="", comment=comment,
        )

    # --- 汇总 ---

    @staticmethod
    def _aggregate(
        field_results: list[FieldCheckResult],
        risk_results: list[AlcoaRiskResult],
        judge_method: str,
        degraded: bool,
        degrade_reason: str | None,
    ) -> DataIntegrityReport:
        present = sum(1 for f in field_results if f.present == "PRESENT")
        field_missing = sum(1 for f in field_results if f.present == "MISSING")
        risk_found = sum(1 for r in risk_results if r.verdict == "RISK")

        critical_missing = [
            f.field
            for f in field_results
            if f.present == "MISSING" and f.severity == RiskLevel.HIGH
        ]
        found_risks = [f"{r.risk}({r.principle})" for r in risk_results if r.verdict == "RISK"]

        verdict = (
            "存在数据可靠性风险"
            if critical_missing or risk_found
            else "未发现明显数据可靠性问题"
        )
        return DataIntegrityReport(
            verdict=verdict,
            judge_method=judge_method,
            degraded=degraded,
            degrade_reason=degrade_reason,
            field_total=len(field_results),
            field_present=present,
            field_missing=field_missing,
            critical_missing_fields=critical_missing,
            risk_total=len(risk_results),
            risk_found=risk_found,
            found_risks=found_risks,
            fields=field_results,
            risks=risk_results,
        )
