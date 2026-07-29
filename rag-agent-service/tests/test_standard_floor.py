"""标准底线核查(3.2a)测试:纯规则比对,离线,不调模型。

用 golden cases.json 逐条验证方向性判定:达标(PASS)/低于底线(FAIL)/无法判定(UNKNOWN)。
比对是确定性规则,所以这里能像单元测试一样断言每条预期 verdict。
"""
import json
from pathlib import Path

from app.knowledge.config_loader import load_regulation_floors, load_grade_orders
from app.review.standard_floor_reviewer import StandardFloorReviewer

GOLDEN = Path(__file__).parent / "golden" / "standard_floor" / "cases.json"


def _make_reviewer() -> StandardFloorReviewer:
    return StandardFloorReviewer(load_regulation_floors(), load_grade_orders())


def test_golden_cases_all_match_expected() -> None:
    """golden 每条断言的判定结果应与标注一致。"""
    reviewer = _make_reviewer()
    cases = json.loads(GOLDEN.read_text(encoding="utf-8"))["cases"]
    mismatches = []
    for c in cases:
        result = reviewer.judge_assertion(c["attribute"], c["value"])
        if result["verdict"] != c["expected_verdict"]:
            mismatches.append(
                f"{c['name']}: 期望 {c['expected_verdict']} 实得 {result['verdict']} ({result['reason']})"
            )
    assert not mismatches, "判定与标注不符:\n" + "\n".join(mismatches)


def test_grade_stricter_passes() -> None:
    """企业 A 级严于底线 D 级 → PASS。"""
    reviewer = _make_reviewer()
    r = reviewer.judge_assertion("洁净度等级", "A级")
    assert r["verdict"] == "PASS"


def test_grade_below_floor_fails() -> None:
    """洁净度底线 D 级已是最低;构造一个不在序内的等级应 UNKNOWN 而非误判。"""
    reviewer = _make_reviewer()
    r = reviewer.judge_assertion("洁净度等级", "E级")
    assert r["verdict"] == "UNKNOWN"


def test_numeric_smaller_stricter() -> None:
    """纯化水微生物 50 ≤ 底线 100 → PASS;150 > 100 → FAIL。"""
    reviewer = _make_reviewer()
    assert reviewer.judge_assertion("纯化水微生物限度", "50 CFU/mL")["verdict"] == "PASS"
    assert reviewer.judge_assertion("纯化水微生物限度", "150 CFU/mL")["verdict"] == "FAIL"


def test_unknown_attribute_not_in_table() -> None:
    """不在底线表的属性 → UNKNOWN,交人工,不瞎判。"""
    reviewer = _make_reviewer()
    r = reviewer.judge_assertion("某个法规没规定的指标", "42")
    assert r["verdict"] == "UNKNOWN"


def test_unparseable_value_is_unknown() -> None:
    """值里没有可比数值 → UNKNOWN,不崩、不瞎判。"""
    reviewer = _make_reviewer()
    r = reviewer.judge_assertion("纯化水微生物限度", "符合规定")
    assert r["verdict"] == "UNKNOWN"
