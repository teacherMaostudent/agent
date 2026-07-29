"""逆向生成:按文件类型 + 补充说明(+ 参照文件),生成带法规依据的 GMP 文件初稿,
并做"生成→自检→自动修订"闭环。

与"审查"相反的方向:审查是"企业写好了 → 判合不合规";生成是"还没写 →
调清单 + 法规库 + 大模型产出符合法规的初稿"。流程:
1. 按 document_type 取该类文件应满足的清单条目(复用审查侧的分类过滤)。
2. 每条要求去法规库捞对应法规原文,作为写作依据(真库在起作用的体现)。
3. 有参照文件时,把其正文一并给模型,让它"参照你的内容重写成合规版"。
4. 把「要求 + 法规依据 + 参照 + 用户补充」组织成 prompt,经 llm-gateway 生成正文。
5. 生成的初稿跑自检(审查);若仍高风险且有可改问题,把具体问题喂回模型修订,
   最多 max_revisions 轮 → 生成即自检即修订闭环。

网关不可用或逻辑模型未注册时抛出,由 API 层给出明确提示。
"""
from dataclasses import dataclass, field

from app.domain.models import ChecklistItem, ReviewResult
from app.generation.llm_client import LlmChatClient
from app.knowledge.repository import InMemoryRepository
from app.retrieval.semantic_retriever import SemanticRetriever
from app.review.gmp_reviewer import GmpReviewService


@dataclass
class GenerationResult:
    document_type: str
    supplement: str
    requirements_used: list[str]      # 参与生成的清单条目标题
    regulation_refs: list[str]        # 引用到的法规来源
    content_markdown: str             # 生成的初稿正文(修订后的最终版)
    self_check_summary: str           # 最终自检结论摘要
    self_check_overall_risk: str      # 最终自检总体风险
    self_check_missing: list[str]     # 最终自检发现的缺失要点
    revision_rounds: int = 0          # 实际修订轮数(0 = 未修订)
    risk_trace: list[str] = field(default_factory=list)  # 每轮风险,看降风险过程
    remaining_issues: list[str] = field(default_factory=list)  # 改不动、留给人工的问题
    used_reference: bool = False      # 是否参照了上传文件
    highlight_terms: list[str] = field(default_factory=list)  # 正文中需加粗+黄底标出的词(残留模糊词/术语变体)


class DocumentGenerator:
    def __init__(
        self,
        repository: InMemoryRepository,
        semantic_retriever: SemanticRetriever,
        chat_client: LlmChatClient,
        reviewer: GmpReviewService,
        max_revisions: int = 3,
    ) -> None:
        self.repository = repository
        self.semantic = semantic_retriever
        self.chat = chat_client
        self.reviewer = reviewer
        self.max_revisions = max_revisions

    def generate(
        self,
        document_type: str,
        supplement: str = "",
        reference_text: str = "",
        revise: bool = True,
    ) -> GenerationResult:
        items = self.repository.checklist_for_document_type(document_type)
        reg_context, reg_refs = self._collect_regulation_context(items)

        # 第一版:参照文件(可选) + 要求 + 法规 + 补充 → 初稿。
        system_prompt = self._system_prompt()
        user_prompt = self._user_prompt(document_type, supplement, items, reg_context, reference_text)
        content = self.chat.complete(system_prompt, user_prompt).strip()
        review = self._self_check(content, document_type)
        risk_trace = [str(review.overall_risk)]

        # 修订闭环(清到底):只要还能提取到可改问题(模糊词/术语/缺失要素)就继续改,
        # 不再只盯总体高风险。加防空转守卫:一轮后问题数没减少(模型改不动或越改越多)
        # 就停,避免浪费轮次和 API 费用。最多 max_revisions 轮。
        rounds = 0
        if revise:
            issues = self._collect_issues(review)
            while rounds < self.max_revisions and issues:
                content = self.chat.complete(
                    system_prompt,
                    self._revision_prompt(document_type, content, issues),
                ).strip()
                review = self._self_check(content, document_type)
                risk_trace.append(str(review.overall_risk))
                rounds += 1
                next_issues = self._collect_issues(review)
                if len(next_issues) >= len(issues):
                    issues = next_issues
                    break  # 没改动或反而变多,停止,剩下的交人工
                issues = next_issues

        missing = [point for dim in review.dimensions for point in dim.missing_points]
        return GenerationResult(
            document_type=document_type,
            supplement=supplement,
            requirements_used=[item.title for item in items],
            regulation_refs=reg_refs,
            content_markdown=content,
            self_check_summary=review.summary,
            self_check_overall_risk=str(review.overall_risk),
            self_check_missing=missing,
            revision_rounds=rounds,
            risk_trace=risk_trace,
            remaining_issues=self._collect_issues(review),
            highlight_terms=self._collect_highlight_terms(review),
            used_reference=bool(reference_text.strip()),
        )

    def _self_check(self, content: str, document_type: str) -> ReviewResult:
        """把初稿当企业文件跑一遍审查,复用同一套分类过滤。"""
        return self.reviewer.review(
            document_id="generated_draft",
            text=content,
            document_type=document_type,
        )

    @staticmethod
    def _collect_issues(review: ReviewResult) -> list[str]:
        """从自检结果提取可喂回模型的具体、可执行问题。

        覆盖率缺失、模糊词(带建议)、术语混用、数据可靠性风险都转成明确指令。
        提取不到(纯人工判断项)时返回空,调用方据此停止修订。
        """
        issues: list[str] = []
        for dim in review.dimensions:
            for point in dim.missing_points:
                issues.append(f"补全「{dim.title}」缺少的要素:{point}")
        clarity = review.clarity
        if clarity:
            for vf in clarity.vague_findings:
                issues.append(
                    f'把模糊词「{vf.word}」改成具体标准(建议:{vf.suggestion or "给出明确数值/条件"})'
                    f';出现处:{vf.context}'
                )
            for ti in clarity.term_inconsistencies:
                issues.append(
                    f'术语统一:「{ti.canonical}」全文只用这一种写法,'
                    f'不要混用 {"、".join(ti.variants_found)}'
                )
        di = review.data_integrity
        if di:
            for f in di.critical_missing_fields:
                issues.append(f"补充数据可靠性关键要素:{f}")
            for r in di.found_risks:
                issues.append(f"消除数据可靠性风险:{r}(明确合规做法,如禁止事后补记、涂改留痕)")
        # 去重保序
        seen: set[str] = set()
        unique: list[str] = []
        for it in issues:
            if it not in seen:
                seen.add(it)
                unique.append(it)
        return unique

    @staticmethod
    def _collect_highlight_terms(review: ReviewResult) -> list[str]:
        """从自检结果抠出正文里要加粗+黄底的"干净词"：残留模糊词 + 混用术语的各写法。

        与 _collect_issues 不同：那个给模型看的是整句指令；这个给 Word 导出用，
        必须是能在正文里精确匹配到的词本身，不能带说明文字。
        """
        terms: set[str] = set()
        clarity = review.clarity
        if clarity:
            for vf in clarity.vague_findings:
                if vf.word:
                    terms.add(vf.word)
            for ti in clarity.term_inconsistencies:
                for v in ti.variants_found:
                    if v:
                        terms.add(v)
        return sorted(terms, key=len, reverse=True)  # 长词优先，避免短词切断长词

    def _collect_regulation_context(
        self, items: list[ChecklistItem]
    ) -> tuple[str, list[str]]:
        """为每条要求从法规库捞对应原文,拼成写作依据。库空时退化为只用清单描述。"""
        blocks: list[str] = []
        refs: set[str] = set()
        for item in items:
            evidence = self.semantic.search_regulations(
                query=f"{item.title} {item.description}", top_k=2
            )
            for ev in evidence:
                standard = ev.metadata.get("standard", "法规")
                refs.add(standard)
                blocks.append(f"【{standard}】{ev.text[:300]}")
            for ref in item.regulation_refs:
                refs.add(ref)
        context = "\n".join(blocks) if blocks else "(法规库未建或无匹配,请依据通用 GMP 原则撰写)"
        return context, sorted(refs)

    @staticmethod
    def _system_prompt() -> str:
        return (
            "你是资深 GMP 质量体系文件起草专家。任务:根据给定的法规强制要求和法规原文依据,"
            "起草一份符合中国 GMP(2010修订)及相关标准的企业质量文件初稿。\n"
            "要求:\n"
            "- 用规范的 SOP/管理规程格式:标题、目的、适用范围、职责、术语定义、正文条款、"
            "记录与附件等,按文件类型裁剪。\n"
            "- 覆盖所有列出的强制要求及其必填字段/要素,不遗漏。\n"
            "- 关键条款后用括号标注法规依据(如:依据《药品生产质量管理规范》第X条)。\n"
            "- 语言准确、可执行,严禁使用'适当''定期''必要时''酌情'等模糊表述,一律给出"
            "具体数值、频次或明确条件(如'每季度至少一次'而非'定期')。\n"
            "- 同一概念全程使用统一术语,不要混用近义词。\n"
            "- 直接输出 Markdown 正文,不要额外解释、不要代码围栏。"
        )

    @staticmethod
    def _user_prompt(
        document_type: str,
        supplement: str,
        items: list[ChecklistItem],
        reg_context: str,
        reference_text: str = "",
    ) -> str:
        req_lines = "\n".join(
            f"- {item.title}:{item.description}"
            + (f"(必含要素:{'、'.join(item.required_fields)})" if item.required_fields else "")
            for item in items
        )
        supplement_block = supplement.strip() or "(无额外补充,按通用情形撰写)"
        parts = [
            f"【文件类型】{document_type}\n",
            f"【必须覆盖的强制要求】\n{req_lines}\n",
            f"【可参考的法规原文依据】\n{reg_context}\n",
        ]
        if reference_text.strip():
            # 参照文件可能很长,截断避免超出上下文,取前 6000 字通常已覆盖文件主体。
            ref = reference_text.strip()[:6000]
            parts.append(
                "【企业提供的参照文件(在其基础上重写,保留其合理内容与企业实际信息,"
                "补齐缺失、修正不合规、消除模糊表述)】\n" + ref + "\n"
            )
        parts.append(f"【企业补充说明(需体现在文件中)】\n{supplement_block}\n")
        parts.append("请据此起草完整的文件初稿。")
        return "\n".join(parts)

    @staticmethod
    def _revision_prompt(document_type: str, current: str, issues: list[str]) -> str:
        """把自检发现的具体问题,连同当前稿,交给模型定向修订。"""
        issue_lines = "\n".join(f"{i + 1}. {it}" for i, it in enumerate(issues))
        return (
            f"下面是一份《{document_type}》初稿,自检发现若干合规问题。请在保留原有结构和"
            "合理内容的前提下,逐条修正以下问题,输出修订后的完整 Markdown 正文。\n"
            "严格要求:\n"
            "- 只输出文档本身。绝对不要把下面这些'待修正问题'或任何审查说明、修改记录"
            "写进正文(尤其不要写进'修订历史/修订内容'表格),否则会污染文档。\n"
            "- '修订历史'表格如需保留,'修订内容'一栏只写业务性描述(如'首次发布'),"
            "不得出现'替换模糊词''统一术语'等审查用语。\n"
            "- 不要解释、不要代码围栏、不要只给改动片段,直接给完整正文。\n\n"
            f"【待修正问题(仅供你修改,不得写入文档)】\n{issue_lines}\n\n"
            f"【当前初稿】\n{current}"
        )
