"""评估器自检(提交 0 的灵魂)。

被测对象是"尺子"本身,不是跨文档 reviewer(提交 2 才有)。这里用手工构造的
"预测"喂进评估器,验证它在四种情形下读数正确:
1. 完美命中 → 召回 1.0、误报 0
2. 漏检 → 召回下降
3. 误报 → clean 组误报数上升、precision 下降
4. clean 组正常 → 召回视为 1.0、误报 0

如果尺子在这些已知情形下读数都不对,那后面拿它量 reviewer 的一切数字都不可信。
"""
from app.evaluation.cross_document_eval import (
    aggregate,
    evaluate_case,
    evaluate_suite,
)


# 一个"有矛盾"的标注:洁净度等级在 A、B 两份文件间打架。
_EXPECTED_CONFLICT = {
    "set_name": "case1_conflict",
    "expected_conflicts": [
        {"topic": "洁净度等级", "files": ["A_洁净区管理规程.txt", "B_无菌灌装操作规程.txt"]},
    ],
}

# 一个"全 clean"的标注:没有任何应报矛盾。
_EXPECTED_CLEAN = {
    "set_name": "case2_clean",
    "expected_conflicts": [],
}


def test_perfect_hit() -> None:
    """预测正好命中标注 → 召回 1.0、精确 1.0、零误报。"""
    prediction = {
        "conflicts": [
            {"topic": "洁净度等级", "files": ["B_无菌灌装操作规程.txt", "A_洁净区管理规程.txt"]},
        ]
    }
    m = evaluate_case(_EXPECTED_CONFLICT, prediction)
    assert m.recall == 1.0
    assert m.precision == 1.0
    assert m.false_positive_count == 0
    # 文件对无序:A-B 与 B-A 必须视为同一条,否则命中判定会漏。
    assert m.hit_count == 1


def test_miss_lowers_recall() -> None:
    """标注有 1 条矛盾,预测什么都没报 → 召回 0。"""
    m = evaluate_case(_EXPECTED_CONFLICT, {"conflicts": []})
    assert m.recall == 0.0
    assert m.false_positive_count == 0


def test_false_positive_on_clean_case() -> None:
    """clean 组标注无矛盾,预测却报了一条 → 记为误报,precision=0。"""
    prediction = {
        "conflicts": [
            {"topic": "温湿度", "files": ["A_变更控制管理规程.txt", "B_偏差处理管理规程.txt"]},
        ]
    }
    m = evaluate_case(_EXPECTED_CLEAN, prediction)
    assert m.false_positive_count == 1
    assert m.precision == 0.0
    assert m.has_expected_conflict is False


def test_clean_case_no_prediction_is_perfect() -> None:
    """clean 组标注无矛盾,预测也没报 → 召回视为 1.0、零误报。"""
    m = evaluate_case(_EXPECTED_CLEAN, {"conflicts": []})
    assert m.recall == 1.0
    assert m.false_positive_count == 0


def test_duplicate_prediction_not_double_counted() -> None:
    """同一条标注矛盾被预测多次,只算一次命中,避免虚高召回。"""
    prediction = {
        "conflicts": [
            {"topic": "洁净度等级", "files": ["A_洁净区管理规程.txt", "B_无菌灌装操作规程.txt"]},
            {"topic": "洁净度等级", "files": ["A_洁净区管理规程.txt", "B_无菌灌装操作规程.txt"]},
        ]
    }
    m = evaluate_case(_EXPECTED_CONFLICT, prediction)
    assert m.hit_count == 1
    assert m.recall == 1.0
    # 两次预测里有一次是重复 → precision 反映"报多了"。
    assert m.precision == 0.5


def test_suite_separates_conflict_and_clean() -> None:
    """套件汇总必须把'有矛盾组召回'和'clean 组误报'分开,不合成单一准确率。"""
    conflict_pred = {
        "conflicts": [
            {"topic": "洁净度等级", "files": ["A_洁净区管理规程.txt", "B_无菌灌装操作规程.txt"]},
        ]
    }
    clean_pred = {"conflicts": []}
    report = evaluate_suite([(_EXPECTED_CONFLICT, conflict_pred), (_EXPECTED_CLEAN, clean_pred)])
    assert report.conflict_case_recall == 1.0
    assert report.conflict_case_count == 1
    assert report.clean_case_false_positives == 0
    assert report.clean_case_count == 1


def test_aggregate_reports_variance() -> None:
    """多轮采样:召回时高时低时,aggregate 必须报出非零方差(Q4)。"""
    conflict_pred_hit = {
        "conflicts": [
            {"topic": "洁净度等级", "files": ["A_洁净区管理规程.txt", "B_无菌灌装操作规程.txt"]},
        ]
    }
    conflict_pred_miss = {"conflicts": []}
    run_hit = evaluate_suite([(_EXPECTED_CONFLICT, conflict_pred_hit)])
    run_miss = evaluate_suite([(_EXPECTED_CONFLICT, conflict_pred_miss)])
    agg = aggregate([run_hit, run_miss])
    assert agg["runs"] == 2
    assert agg["recall_mean"] == 0.5
    # 一轮 1.0、一轮 0.0 → 方差必须非零,证明它能暴露判定不稳。
    assert agg["recall_stdev"] > 0
