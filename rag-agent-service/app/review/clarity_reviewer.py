"""表述清晰度核查(设计文档 3.7)。

纯规则、不依赖大模型,性价比最高的维度:
- 模糊词扫描:命中"适当/定期/酌情/必要时"等禁用/慎用词,给整改建议 + 上下文。
- 术语统一:同一概念在文件里若混用多种写法(如"偏差"又"异常"),提示统一。

用最长匹配优先(先长词后短词),并对每个模糊词只取前若干处上下文,避免报告过长。
"""

import re

from app.domain.models import (
    ClarityReport,
    RiskLevel,
    TermInconsistency,
    VagueFinding,
)

# 每个模糊词最多展示的命中处数(避免同一个词刷屏)。
_MAX_CONTEXT_PER_WORD = 3
# 句子边界:命中词所在的完整句子按这些标点切分,取整句当上下文(不再硬切固定字数)。
_SENTENCE_BOUNDARY = "。！？；\n；;!?"
# 整句过长时的兜底上限,避免个别超长句撑爆展示。
_MAX_CONTEXT_LEN = 80


class ClarityReviewer:
    def __init__(self, vague_words: list[dict], term_groups: list[dict]) -> None:
        self.vague_words = vague_words
        self.term_groups = term_groups

    def review(self, text: str) -> ClarityReport:
        vague_findings = self._scan_vague(text)
        term_issues = self._scan_terms(text)
        verdict = self._verdict(vague_findings, term_issues)
        return ClarityReport(
            verdict=verdict,
            vague_count=len(vague_findings),
            vague_findings=vague_findings,
            term_issue_count=len(term_issues),
            term_inconsistencies=term_issues,
        )

    def _scan_vague(self, text: str) -> list[VagueFinding]:
        findings: list[VagueFinding] = []
        for entry in self.vague_words:
            word = entry.get("word", "")
            if not word:
                continue
            positions = [m.start() for m in re.finditer(re.escape(word), text)]
            if not positions:
                continue
            severity = self._severity(entry.get("severity"))
            for pos in positions[:_MAX_CONTEXT_PER_WORD]:
                findings.append(
                    VagueFinding(
                        word=word,
                        suggestion=entry.get("suggestion", ""),
                        severity=severity,
                        context=self._context(text, pos, len(word)),
                    )
                )
        return findings

    def _scan_terms(self, text: str) -> list[TermInconsistency]:
        """一个概念的规范词 + 同义词里,文件中出现了 ≥2 种写法 → 判为混用。"""
        issues: list[TermInconsistency] = []
        for group in self.term_groups:
            canonical = group.get("canonical", "")
            synonyms = group.get("synonyms", [])
            all_terms = [canonical, *synonyms]
            found = [t for t in all_terms if t and t in text]
            if len(found) >= 2:
                issues.append(
                    TermInconsistency(
                        canonical=canonical,
                        variants_found=found,
                        note=group.get("note", ""),
                    )
                )
        return issues

    @staticmethod
    def _context(text: str, pos: int, word_len: int) -> str:
        """取命中词所在的完整句子作上下文,按中文标点/换行断句,不再拦腰硬切。"""
        # 向前找到句子起点(上一个句末标点之后)。
        start = 0
        for j in range(pos - 1, -1, -1):
            if text[j] in _SENTENCE_BOUNDARY:
                start = j + 1
                break
        # 向后找到句子终点(命中词之后的第一个句末标点,含标点本身)。
        end = len(text)
        for j in range(pos + word_len, len(text)):
            if text[j] in _SENTENCE_BOUNDARY:
                end = j + 1
                break
        sentence = text[start:end].strip()
        # 整句仍过长时,以命中词为中心裁一段,避免展示区被超长句撑爆。
        if len(sentence) > _MAX_CONTEXT_LEN:
            rel = pos - start
            left = max(0, rel - _MAX_CONTEXT_LEN // 2)
            right = min(len(sentence), left + _MAX_CONTEXT_LEN)
            prefix = "…" if left > 0 else ""
            suffix = "…" if right < len(sentence) else ""
            return f"{prefix}{sentence[left:right].strip()}{suffix}"
        return sentence

    @staticmethod
    def _severity(label: str | None) -> RiskLevel:
        mapping = {"高": RiskLevel.HIGH, "中": RiskLevel.MEDIUM, "低": RiskLevel.LOW}
        # 模糊词默认按中风险(建议整改但非致命)。
        return mapping.get((label or "").strip(), RiskLevel.MEDIUM)

    @staticmethod
    def _verdict(vague: list[VagueFinding], terms: list[TermInconsistency]) -> str:
        if not vague and not terms:
            return "表述清晰,未发现明显模糊词或术语混用"
        parts = []
        if vague:
            parts.append(f"发现 {len(vague)} 处模糊表述")
        if terms:
            parts.append(f"{len(terms)} 组术语混用")
        return "、".join(parts) + ",建议按建议整改以提升可执行性"
