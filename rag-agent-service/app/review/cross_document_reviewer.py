"""跨文档聚合审查(组件⑥):对一组企业文件做两阶段编排,查"多文件矛盾"和
"职责分离冲突"。见 [[gmp-cross-document-plan]]。

设计要点(对应 14 问的关键修改)：
- 一致性线：双通道召回(向量保底 + 关键词全文扫)→ 结构化片段 → LLM 判"同场景才算矛盾"。
- 职责线：先抽职责矩阵(角色×动作)→ 规则粗筛求交(operator ∩ reviewer)→ 只"提示"不"裁决",
  规则判不了的边界交 LLM 兜底(Q8/Q9)。
- 版本识别：同一文件的不同版本默认不互相比对(Q14)。
- 定位：系统"堆疑点",不"下最终裁决",所有发现标 need_human_review。
"""
import re

from app.domain.models import CrossDocEvidence, CrossDocFinding, CrossDocReport
from app.retrieval.semantic_retriever import SemanticRetriever

# 从文件名/文本里认版本号：v1 / V2.0 / 版本 3 / 第2版。
_VERSION_RE = re.compile(r"(?:v|V|版本|第)\s*([0-9]+(?:\.[0-9]+)?)")
# 职责矩阵：动作词 → 该动作的责任人角色。抽取时按这些动作在原文附近找人名/岗位。
_ACTIONS = ["操作", "执行", "复核", "审核", "批准", "放行", "记录"]


class CrossDocumentReviewer:
    def __init__(self, semantic_retriever: SemanticRetriever, judge=None, vote_n: int = 3) -> None:
        self.semantic = semantic_retriever
        self.judge = judge  # 可为 None：职责走纯规则、一致性无法判时跳过(不臆测)
        # 自一致性投票次数：同一文件抽 N 次，只留多数次都出现的断言(压 LLM 抽取波动)。
        # 1 = 关闭投票(离线测试用，省 token 且行为确定)。
        self.vote_n = max(1, vote_n)

    def review(
        self,
        files: list[tuple[str, str, str]],
        numeric_topics: list[dict],
        responsibility_topics: list[dict] | None = None,
    ) -> CrossDocReport:
        """files: [(document_id, filename, text)]。

        numeric_topics: 数值/标准型主题。此版仅用作"抽取时关注哪些属性"的提示，
            不再驱动检索(旧的主题驱动检索会串味、重复、有盲区，见 gmp-cross-document-plan)。
        responsibility_topics: 职责型主题(走职责矩阵 + 规则粗筛)。
        同名文件的不同版本先剔除(只留每个文件名基名的最高版本),避免版本差异被误报。

        一致性线改为"断言抽取 + 规则比对"：每份文件各自抽 (对象,属性,值) 三元组，
        再按 (对象,属性) 分组，同组出现 ≥2 个不同值即矛盾。判断是纯规则、确定性，
        LLM 只做它擅长的"文本→结构化断言"抽取。
        """
        deduped = self._drop_old_versions(files)
        attribute_hints = [t.get("topic", "") for t in numeric_topics if t.get("topic")]

        consistency = self._check_consistency(deduped, attribute_hints)

        # 职责检查扫全部文件、与具体主题无关，有职责主题时只跑一次。
        responsibility: list[CrossDocFinding] = []
        checked = list(attribute_hints)
        if responsibility_topics:
            checked.extend(t.get("topic", "") for t in responsibility_topics)
            responsibility.extend(self._check_responsibility(deduped))

        total = len(consistency) + len(responsibility)
        verdict = (
            f"跨 {len(deduped)} 份文件核查 {len(checked)} 个主题，"
            f"发现 {total} 处待人工确认（矛盾 {len(consistency)}、职责 {len(responsibility)}）。"
            if total
            else f"跨 {len(deduped)} 份文件未发现明显矛盾或职责冲突。"
        )
        # 给每条发现分配【本快照内唯一】的 local_id(f1、f2…)。故意不跨快照——
        # 人工标注只属于这一次快照,不做跨会话对齐,从根上绕开"稳定id做不到"的死结。
        for i, f in enumerate([*consistency, *responsibility], start=1):
            f.local_id = f"f{i}"
        return CrossDocReport(
            verdict=verdict,
            document_ids=[doc_id for doc_id, _, _ in deduped],
            consistency_findings=consistency,
            responsibility_findings=responsibility,
            topics_checked=checked,
        )

    # --- 版本识别 (Q14) ---

    @staticmethod
    def _base_name(filename: str) -> str:
        """去掉版本号和扩展名,得到"同一份文件"的基名。"""
        name = re.sub(r"\.(txt|md|pdf|docx?|xlsx?)$", "", filename, flags=re.I)
        return _VERSION_RE.sub("", name).strip(" _-")

    @staticmethod
    def _version_of(filename: str) -> float:
        m = _VERSION_RE.search(filename)
        return float(m.group(1)) if m else 0.0

    def _drop_old_versions(
        self, files: list[tuple[str, str, str]]
    ) -> list[tuple[str, str, str]]:
        """同基名只留最高版本;不同基名全保留。"""
        best: dict[str, tuple[str, str, str]] = {}
        for doc_id, filename, text in files:
            base = self._base_name(filename)
            if base not in best or self._version_of(filename) > self._version_of(best[base][1]):
                best[base] = (doc_id, filename, text)
        return list(best.values())

    # --- 一致性线 (断言抽取 + 规则比对;判断是确定性的,不靠模型每次发挥) ---

    def _check_consistency(
        self,
        files: list[tuple[str, str, str]],
        attribute_hints: list[str],
    ) -> list[CrossDocFinding]:
        """每份文件各自抽"合规断言"三元组 → 按(对象,属性)分组 → 组内出现≥2个
        不同值即矛盾。LLM 只做"文本→断言"抽取,判矛盾是纯规则,故:
        - 不串味:洁净度的值只和洁净度的值比,不会跟"检验频次"归到一组。
        - 不重复:一个(对象,属性)只比一次。
        - 确定可复现:比对无随机性。
        """
        if self.judge is None:
            return []  # 抽取必须靠模型;无模型不臆测

        # 第一步:逐文件抽断言,每份抽 vote_n 次取多数(self-consistency),
        # 过滤 LLM 单次采样的抖动(如偶发的湿度误抽)。见问题二解法。
        assertions: list[dict] = []
        for doc_id, filename, text in files:
            for a in self._extract_with_voting(filename, text, attribute_hints):
                obj = self._norm(a.get("object"))
                attr = self._norm(a.get("attribute"))
                val = str(a.get("value", "")).strip()
                if obj and attr and val:
                    assertions.append({
                        "object": obj, "attribute": attr, "value": val,
                        "value_norm": self._norm_value(val),
                        "quote": a.get("quote", ""), "filename": filename, "document_id": doc_id,
                    })

        # 第二步:按(对象,属性)分组,组内规范化后不同值 = 矛盾(纯规则)。
        groups: dict[tuple[str, str], list[dict]] = {}
        for a in assertions:
            groups.setdefault((a["object"], a["attribute"]), []).append(a)

        findings: list[CrossDocFinding] = []
        for (obj, attr), items in groups.items():
            # 用【规范化后】的值判异同：'≤65%'与'不超过65%'视为相同,不误报。
            by_norm: dict[str, dict] = {}
            for it in items:
                by_norm.setdefault(it["value_norm"], it)
            distinct_files = {it["filename"] for it in items}
            if len(by_norm) >= 2 and len(distinct_files) >= 2:
                reps = list(by_norm.values())
                evidence = [
                    CrossDocEvidence(
                        document_id=it["document_id"], filename=it["filename"], quote=it["quote"]
                    )
                    for it in reps
                ]
                values_desc = "、".join(f"{it['filename']}:{it['value']}" for it in reps)
                pair = sorted({it["filename"] for it in reps})
                findings.append(
                    CrossDocFinding(
                        finding_type="consistency",
                        obj=obj,
                        document_pair=pair,
                        topic=attr,
                        summary=f"「{obj}」的「{attr}」在多份文件中不一致（{values_desc}）",
                        evidence=evidence,
                        detail="同一对象同一属性出现不同取值，请人工确认是否为真矛盾。",
                        need_human_review=True,
                    )
                )
        return findings

    def _extract_with_voting(
        self, filename: str, text: str, attribute_hints: list[str]
    ) -> list[dict]:
        """抽 vote_n 次,只保留在【多数次】里都出现的断言(self-consistency)。

        身份键 = (对象归一, 属性归一, 值规范化)。出现次数 > vote_n//2 才留。
        代表条取该键第一次出现的原始断言(保留可读的原值和原话)。
        """
        counts: dict[tuple[str, str, str], int] = {}
        reps: dict[tuple[str, str, str], dict] = {}
        for _ in range(self.vote_n):
            try:
                result = self.judge.extract_assertions(filename, text, attribute_hints)
            except Exception:
                continue  # 单次失败不影响其余轮
            seen_this_round: set[tuple[str, str, str]] = set()
            for a in result.get("assertions", []):
                key = (
                    self._norm(a.get("object")),
                    self._norm(a.get("attribute")),
                    self._norm_value(str(a.get("value", ""))),
                )
                if not all(key):
                    continue
                if key in seen_this_round:
                    continue  # 同一轮内重复只计一次
                seen_this_round.add(key)
                counts[key] = counts.get(key, 0) + 1
                reps.setdefault(key, a)
        threshold = self.vote_n // 2 + 1  # 多数
        return [reps[k] for k, c in counts.items() if c >= threshold]

    @staticmethod
    def _norm(s: str | None) -> str:
        """对象/属性归一:去空白、去标点,便于分组匹配(缓解'灌装间'vs'灌装间 '之类)。"""
        return re.sub(r"[\s　:：,，。()（）]", "", s or "").strip()

    @staticmethod
    def _norm_value(v: str | None) -> str:
        """值规范化:让语义相同的值判为相等,避免'≤65%'vs'不超过65%'误报。

        - 统一"不超过/不大于/≤/<="→"≤","不低于/不小于/≥/>="→"≥";
        - 全角→半角常见符号;去空白与波浪号方向差异(~/～)。
        """
        s = (v or "").strip()
        repl = {
            "不超过": "≤", "不大于": "≤", "<=": "≤", "≤": "≤",
            "不低于": "≥", "不小于": "≥", ">=": "≥", "≥": "≥",
            "～": "~", "－": "-", "℃": "℃", "％": "%",
        }
        for a, b in repl.items():
            s = s.replace(a, b)
        return re.sub(r"[\s　]", "", s)

    # --- 职责线 (规则粗筛 + LLM 兜底;只提示不裁决 Q8/Q9) ---

    def _check_responsibility(
        self, files: list[tuple[str, str, str]]
    ) -> list[CrossDocFinding]:
        matrix = self._build_responsibility_matrix(files)
        findings = self._rule_screen_responsibility(matrix)
        return findings

    def _build_responsibility_matrix(
        self, files: list[tuple[str, str, str]]
    ) -> list[dict]:
        """粗抽职责矩阵:以"由"字为锚抓责任人,动作类别看"由"前最近的动作词。
        返回 [{action, actor, filename}]。

        为什么锚"由"(对应 Q8):满文找人名极易误抽(把"操作人张"当人名)。GMP 文件里
        职责句式高度固定——"……由(角色)某某执行/复核/完成"。锚定"由"字,只取其后紧邻的
        2-3 字人名(剥离"操作人/复核人"等角色词),宁可漏抽也不乱抽,把误报压到最低。
        """
        rows: list[dict] = []
        for _doc_id, filename, text in files:
            flat = "".join(text.split())
            for m in re.finditer("由", flat):
                after = flat[m.end() : m.end() + 10]
                actor = self._pick_actor(after)
                if not actor:
                    continue
                # 动作类别:看"由"字前最近的动作词(如"记录的复核由张三"→复核)。
                before = flat[max(0, m.start() - 10) : m.start()]
                action = self._nearest_action(before) or self._nearest_action(after)
                if action:
                    rows.append({"action": action, "actor": actor, "filename": filename})
        return rows

    @staticmethod
    def _pick_actor(after_you: str) -> str:
        """取"由"字后紧邻的责任人:剥离角色词前缀,抓 2-3 字人名(动词前停)。"""
        # 剥离"操作人/复核人/审核人/批准人"等角色词前缀,留下真正的人名。
        stripped = re.sub(r"^(操作人|复核人|审核人|批准人|放行人|记录人|当班)", "", after_you)
        # 人名后通常紧跟动词(执行/复核/完成/签字…),用它作右边界,避免把动词抓进名字。
        m = re.match(r"([一-龥]{2,3})(?=执行|复核|审核|批准|放行|完成|负责|签字|操作|记录|$)", stripped)
        if m:
            token = m.group(1)
            noise = {"操作", "复核", "审核", "批准", "放行", "记录", "质量", "部门", "本人"}
            if token not in noise:
                return token
        return ""

    @staticmethod
    def _nearest_action(fragment: str) -> str:
        """返回片段里出现的、离"由"最近的一个动作词(操作类/复核类均可)。"""
        best_pos, best_action = -1, ""
        for action in _ACTIONS:
            pos = fragment.rfind(action)
            if pos > best_pos:
                best_pos, best_action = pos, action
        return best_action

    def _rule_screen_responsibility(self, matrix: list[dict]) -> list[CrossDocFinding]:
        """规则粗筛:同一责任人是否既做"操作类"又做"复核类"动作(职责分离)。"""
        operate = {"操作", "执行", "记录"}
        review = {"复核", "审核", "批准", "放行"}
        actor_actions: dict[str, set] = {}
        actor_files: dict[str, set] = {}
        for row in matrix:
            actor_actions.setdefault(row["actor"], set()).add(row["action"])
            actor_files.setdefault(row["actor"], set()).add(row["filename"])

        findings: list[CrossDocFinding] = []
        for actor, actions in actor_actions.items():
            if actions & operate and actions & review:
                files_involved = sorted(actor_files[actor])
                findings.append(
                    CrossDocFinding(
                        finding_type="responsibility",
                        topic="职责分离",
                        summary=(
                            f"疑似职责分离冲突：「{actor}」同时承担操作类"
                            f"({'、'.join(actions & operate)})与复核类"
                            f"({'、'.join(actions & review)})职责。"
                        ),
                        evidence=[
                            CrossDocEvidence(document_id="", filename=fn, quote="")
                            for fn in files_involved
                        ],
                        detail="规则粗筛结果，请人工确认是否为同一人(可能同名不同人/不同产品线)。",
                        need_human_review=True,
                    )
                )
        return findings
