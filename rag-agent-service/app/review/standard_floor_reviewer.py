"""标准底线核查(3.2a:企业标准不得低于法规底线)。见 [[gmp-cross-document-plan]] 同源框架。

框架定位:A 类的变体。复用跨文档那套"断言抽取"抽企业量化标准,比对的另一方
从"企业断言"换成"预建的法规底线表"。判定是【纯规则 + 方向性比较】,不靠 LLM 判达标。

三条铁律(沿用):
1. 能规则化的判定绝不交 LLM——达标与否是方向性数值/等级比较,确定可复现。
2. 系统只堆疑点、标"待人工确认",不下最终裁决。
3. 判不了就诚实标 UNKNOWN(属性不在表 / 值解析不出数值 / 等级不在序内),绝不瞎判。
"""
import re

# 判定结果三态。
PASS = "PASS"        # 达标(等于或严于底线)
FAIL = "FAIL"        # 低于法规底线(硬伤)
UNKNOWN = "UNKNOWN"  # 无法自动判定,交人工


class StandardFloorReviewer:
    def __init__(
        self,
        floors: list[dict],
        grade_orders: dict[str, list[str]],
        judge=None,
        vote_n: int = 3,
    ) -> None:
        self.floors = floors
        self.grade_orders = grade_orders
        self.judge = judge  # 抽企业量化标准用；None 时 review() 跳过(比对逻辑仍可离线单测)
        self.vote_n = vote_n
        # 属性名/别名 → 底线条目,便于抽到断言后快速定位。
        self._index: dict[str, dict] = {}
        for f in floors:
            for name in [f.get("attribute", ""), *f.get("aliases", [])]:
                key = self._norm(name)
                if key:
                    self._index[key] = f

    def review(self, document_id: str, filename: str, text: str):
        """抽企业量化标准 → 逐条判定 → 汇总 StandardFloorReport。

        抽取复用断言框架(extract_assertions),judge=None 或抽取失败则返回空报告
        (不臆测)。只保留能在底线表命中的属性,FAIL 一律 need_human_review。
        """
        from app.domain.models import StandardFloorFinding, StandardFloorReport

        report = StandardFloorReport()
        if self.judge is None:
            report.verdict = "未启用标准底线核查(需大模型抽取企业量化标准)"
            return report
        report.judge_method = "LLM"

        # 提示模型重点关注底线表里的属性(但不限于)。
        hints = [f.get("attribute", "") for f in self.floors if f.get("attribute")]
        try:
            result = self.judge.extract_assertions(filename, text, hints)
        except Exception:
            report.verdict = "企业量化标准抽取失败,已跳过标准底线核查"
            return report

        seen: set[tuple[str, str]] = set()
        for a in result.get("assertions", []):
            attribute = str(a.get("attribute", "")).strip()
            value = str(a.get("value", "")).strip()
            if not attribute or not value:
                continue
            dedup = (self._norm(attribute), self._norm(value))
            if dedup in seen:
                continue
            seen.add(dedup)
            j = self.judge_assertion(attribute, value)
            # 只收录能对上底线表的(UNKNOWN 且无 floor = 属性不在表,不噪扰用户)。
            if j["verdict"] == UNKNOWN and j.get("floor") is None:
                continue
            floor = j.get("floor") or {}
            report.findings.append(
                StandardFloorFinding(
                    attribute=attribute,
                    enterprise_value=value,
                    verdict=j["verdict"],
                    floor_value=str(floor.get("floor", "")) + (" " + floor.get("unit", "") if floor.get("unit") else ""),
                    reason=j["reason"],
                    quote=str(a.get("quote", "")),
                    source=floor.get("source", ""),
                    need_human_review=j["verdict"] in (FAIL, UNKNOWN),
                )
            )

        fails = sum(1 for f in report.findings if f.verdict == FAIL)
        unknowns = sum(1 for f in report.findings if f.verdict == UNKNOWN)
        if not report.findings:
            report.verdict = "未抽取到可对照法规底线的量化标准"
        elif fails:
            report.verdict = f"发现 {fails} 项企业标准低于法规底线，需整改（另有 {unknowns} 项待人工判定）"
        elif unknowns:
            report.verdict = f"未发现低于底线项，有 {unknowns} 项无法自动判定，建议人工核查"
        else:
            report.verdict = f"{len(report.findings)} 项量化标准均不低于法规底线"
        return report

    def judge_assertion(self, attribute: str, value: str) -> dict:
        """判断单条企业断言是否达标。返回 {verdict, floor, reason}。

        verdict ∈ {PASS, FAIL, UNKNOWN}。UNKNOWN 时 floor 可能为 None。
        """
        floor = self._index.get(self._norm(attribute))
        if floor is None:
            return {"verdict": UNKNOWN, "floor": None, "reason": "该属性不在法规底线表中,无法自动判定,建议人工核查"}

        direction = floor.get("direction", "")
        if direction == "grade":
            return self._judge_grade(value, floor)
        if direction in ("smaller_stricter", "larger_stricter"):
            return self._judge_numeric(value, floor, direction)
        return {"verdict": UNKNOWN, "floor": floor, "reason": "底线方向未定义,交人工"}

    # --- 等级型(如洁净度 A>B>C>D) ---

    def _judge_grade(self, value: str, floor: dict) -> dict:
        order = self.grade_orders.get(floor.get("gradeOrderKey", ""), [])
        ent_grade = self._extract_grade(value, order)
        floor_grade = floor.get("floor", "")
        if ent_grade is None or floor_grade not in order:
            return {"verdict": UNKNOWN, "floor": floor, "reason": f"无法从「{value}」识别出有效等级,交人工"}
        # order 越靠前越严(index 小 = 严)。企业 index ≤ 底线 index 即达标(等于或更严)。
        ent_i = order.index(ent_grade)
        floor_i = order.index(floor_grade)
        if ent_i <= floor_i:
            return {"verdict": PASS, "floor": floor, "reason": f"企业「{ent_grade}级」不低于底线「{floor_grade}级」"}
        return {"verdict": FAIL, "floor": floor, "reason": f"企业「{ent_grade}级」低于法规底线「{floor_grade}级」"}

    @staticmethod
    def _extract_grade(value: str, order: list[str]) -> str | None:
        """从'D级''B 级洁净''环境控制级别为C'里抽出等级字母,须在 order 内。"""
        for m in re.finditer(r"([A-Za-z])\s*级", value):
            g = m.group(1).upper()
            if g in order:
                return g
        # 兜底:值本身就是单个等级字母
        v = value.strip().upper()
        return v if v in order else None

    # --- 数值型(越小越严 / 越大越严) ---

    def _judge_numeric(self, value: str, floor: dict, direction: str) -> dict:
        ent_num = self._extract_number(value)
        floor_num = self._extract_number(floor.get("floor", ""))
        if ent_num is None or floor_num is None:
            return {"verdict": UNKNOWN, "floor": floor, "reason": f"无法从「{value}」提取可比数值,交人工"}
        if direction == "smaller_stricter":
            ok = ent_num <= floor_num
            rel = "≤" if ok else ">"
        else:  # larger_stricter
            ok = ent_num >= floor_num
            rel = "≥" if ok else "<"
        unit = floor.get("unit", "")
        if ok:
            return {"verdict": PASS, "floor": floor, "reason": f"企业值 {ent_num}{rel}底线 {floor_num} {unit},达标"}
        return {"verdict": FAIL, "floor": floor, "reason": f"企业值 {ent_num}{rel}底线 {floor_num} {unit},低于法规底线"}

    @staticmethod
    def _extract_number(value: str) -> float | None:
        """从'50 CFU/mL''不超过100''0.5 年'里抽第一个数值。抽不到返回 None。"""
        m = re.search(r"[-+]?\d+(?:\.\d+)?", value or "")
        return float(m.group(0)) if m else None

    @staticmethod
    def _norm(s: str | None) -> str:
        return re.sub(r"[\s　:：,，。()（）]", "", s or "").strip().lower()
