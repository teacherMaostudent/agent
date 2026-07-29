import re

from app.domain.models import ReviewResult


def _flat_quote(text: str, limit: int = 600) -> str:
    """把引文里的换行/回车/多余空白拍平成单空格，让 markdown 的 > 引用块能罩住
    整条（引文内含 \n 会导致第二行掉出引用块、在 Word 里散成别的段落）。"""
    return re.sub(r"\s+", " ", (text or "")[:limit]).strip()


class MarkdownReportRenderer:
    def render(self, review: ReviewResult) -> str:
        lines = [
            f"# GMP 合规审查报告",
            "",
            f"- 审查编号：{review.review_id}",
            f"- 文档编号：{review.document_id}",
            f"- 总体风险：{review.overall_risk}",
            f"- 状态：{review.status}",
            "",
            "## 摘要",
            "",
            review.summary,
            "",
            f"- 条款覆盖率：{('不可计算' if review.coverage.rate is None else f'{review.coverage.rate:.1%}')}",
            f"- 映射状态：{review.mapping_status}",
            *([f"- 映射警告：{review.mapping_warning}"] if review.mapping_warning else []),
            "",
            "## 检查结果",
            "",
        ]
        for item in review.dimensions:
            lines.extend(
                [
                    f"### {item.dimension} {item.title}",
                    "",
                    f"- 覆盖状态：{item.coverage_status}",
                    f"- 判定方式：{item.judge_method}",
                    f"- 是否降级：{'是' if item.degraded else '否'}",
                    *([f"- 降级原因：{item.degrade_reason}"] if item.degrade_reason else []),
                    f"- 风险等级：{item.risk_level}",
                    f"- 缺失点：{('、'.join(item.missing_points) if item.missing_points else '无')}",
                    f"- 法规引用：{('、'.join(item.regulation_refs) if item.regulation_refs else '无')}",
                    f"- 原因说明：{item.reason}",
                    f"- CAPA 建议：{item.capa_suggestion}",
                    f"- 是否需要人工复核：{'是' if item.need_human_review else '否'}",
                    "",
                    "企业文件证据(判断依据)：",
                    "",
                ]
            )
            for evidence in item.evidence[:3]:
                lines.append(f"> {_flat_quote(evidence.text, 150)}")
                lines.append("")
            if item.regulation_evidence:
                lines.append("")
                lines.append("对应法规条文(依据引用)：")
                lines.append("")
                for reg in item.regulation_evidence[:2]:
                    std = reg.metadata.get("regulation") or reg.metadata.get("standard") or reg.source_id
                    lines.append(f"> [{std}] {_flat_quote(reg.text, 300)}")
                    lines.append("")
        lines.extend(self._render_data_integrity(review))
        lines.extend(self._render_clarity(review))
        lines.extend(self._render_standard_floor(review))
        return "\n".join(lines)

    @staticmethod
    def _render_standard_floor(review: ReviewResult) -> list[str]:
        floor = review.standard_floor
        if floor is None or not floor.findings:
            return []
        lines = [
            "## 标准底线核查(3.2a)",
            "",
            f"- 结论：{floor.verdict}",
            f"- 已核查量化标准：{floor.checked_count} 项，低于底线 {floor.fail_count} 项，待人工判定 {floor.unknown_count} 项",
            "",
        ]
        label = {"PASS": "达标", "FAIL": "低于底线", "UNKNOWN": "待人工判定"}
        for f in floor.findings:
            lines.append(
                f"- [{label.get(f.verdict, f.verdict)}]「{f.attribute}」：企业值 {f.enterprise_value}"
                + (f"，法规底线 {f.floor_value}" if f.floor_value.strip() else "")
            )
            lines.append(f"  - {f.reason}" + (f"（依据：{f.source}）" if f.source else ""))
            if f.quote:
                lines.append(f"  > {f.quote}")
        lines.append("")
        return lines

    @staticmethod
    def _render_data_integrity(review: ReviewResult) -> list[str]:
        di = review.data_integrity
        if di is None:
            return []
        lines = [
            "## 数据可靠性(ALCOA+)",
            "",
            f"- 结论：{di.verdict}",
            f"- 判定方式：{di.judge_method}",
            f"- 是否降级：{'是' if di.degraded else '否'}",
            *([f"- 降级原因：{di.degrade_reason}"] if di.degrade_reason else []),
            f"- 字段完整：{di.field_present}/{di.field_total} 项存在，缺失 {di.field_missing} 项",
            f"- 关键缺失字段：{('、'.join(di.critical_missing_fields) if di.critical_missing_fields else '无')}",
            f"- ALCOA+ 风险：{di.risk_found}/{di.risk_total} 项命中",
            f"- 命中风险：{('、'.join(di.found_risks) if di.found_risks else '无')}",
            "",
        ]
        return lines

    @staticmethod
    def _render_clarity(review: ReviewResult) -> list[str]:
        clarity = review.clarity
        if clarity is None:
            return []
        lines = [
            "## 表述清晰度(3.7)",
            "",
            f"- 结论：{clarity.verdict}",
            f"- 模糊表述：{clarity.vague_count} 处",
            f"- 术语混用：{clarity.term_issue_count} 组",
            "",
        ]
        if clarity.vague_findings:
            lines.append("模糊表述明细：")
            lines.append("")
            for f in clarity.vague_findings:
                lines.append(f"- 「{f.word}」（{f.severity}）：{f.suggestion}")
                lines.append(f"  > {f.context}")
            lines.append("")
        if clarity.term_inconsistencies:
            lines.append("术语混用明细：")
            lines.append("")
            for t in clarity.term_inconsistencies:
                variants = "、".join(t.variants_found)
                lines.append(f"- 建议统一为「{t.canonical}」：文件中出现 {variants}。{t.note}")
            lines.append("")
        return lines

