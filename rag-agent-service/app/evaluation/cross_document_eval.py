"""跨文档审查的评估基线(提交 0)。

这是"尺子",不是业务功能。它独立于跨文档 reviewer(提交 2 才有)——只吃
两个轻量结构:标注(golden)和预测(prediction),算出可信的量化指标。这样
reviewer 一做完,接上就能量,而尺子本身可以先立住、先自测。

—— 为什么这么设计(对应之前 14 问的 Q1~Q4)——
- Q3 命中定义写死:预测矛盾 与 标注矛盾,只要 (同 topic + 涉及同两份文件)
  即算召回命中;数值/原话对不对,单列 precision_detail 子指标,不与召回混。
- Q2 类不平衡:有矛盾组只看"召回率",clean 组只看"误报数",分开报,
  绝不合成一个准确率糊弄自己。clean 组误报是对抗告警疲劳的硬指标。
- Q4 随机性:本模块只负责"给定预测→算指标";多次采样(temp=0 跑 N 次)
  由调用方做,再把每次指标传进 aggregate() 看均值±方差。
"""
from dataclasses import dataclass, field


def _pair_key(files: list[str]) -> frozenset[str]:
    """两份文件构成的无序对,作为矛盾的身份。A-B 与 B-A 视为同一对。"""
    return frozenset(files)


def _conflict_key(conflict: dict) -> frozenset[str]:
    """一条矛盾的"身份":就是【文件对】——哪两份文件在打架。用于 Q3 的命中判定。

    刻意【不】用 topic 名,也【不】用 object 名当键——两者都是模型自由抽取、受
    "粒度/切分"影响的展示字段(如同一处矛盾,object 可切成'洁净区'或'悬浮粒子',
    topic 可写'温度范围'或'温湿度限度')。任何自由字符串当硬键,都会把抓到的矛盾
    误判漏检。矛盾的物理本质就是"这两份文件在打架",文件对是最稳定、系统与尺子
    可精确共用的身份。

    边界:一对文件只有一个核心矛盾时,文件对为键足够(当前 golden 全满足)。若一对
    文件出现多个不同对象的矛盾,需把 object 作二级细分键补上——留待那时再加。
    """
    return _pair_key(conflict.get("files", []))


@dataclass
class CaseMetrics:
    """单个 case 的评估结果。"""

    case_name: str
    has_expected_conflict: bool          # 这组是"有矛盾"还是"clean 反例"
    expected_count: int                  # 标注的矛盾数
    predicted_count: int                 # 预测报出的矛盾数
    hit_count: int                       # 命中(topic+文件对 对上)的数
    false_positive_count: int            # 报了但标注里没有的(误报)
    recall: float                        # 召回率 = hit / expected(有矛盾组才有意义)
    precision: float                     # 精确率 = hit / predicted


def evaluate_case(expected: dict, prediction: dict) -> CaseMetrics:
    """比对单个 case 的标注与预测,算召回/误报/精确率。

    expected: golden 的 expected.json,含 expected_conflicts / should_be_clean。
    prediction: 跨文档 reviewer 的输出,统一成 {"conflicts": [{topic, files, ...}]}。
    """
    exp_conflicts = expected.get("expected_conflicts", [])
    pred_conflicts = prediction.get("conflicts", [])

    exp_keys = {_conflict_key(c) for c in exp_conflicts}
    pred_keys = [_conflict_key(c) for c in pred_conflicts]

    # 命中去重:同一标注矛盾被多次预测只算一次命中,避免虚高召回。
    hit_unique = len(exp_keys & set(pred_keys))
    false_positive = sum(1 for k in pred_keys if k not in exp_keys)

    expected_count = len(exp_keys)
    predicted_count = len(pred_conflicts)
    recall = hit_unique / expected_count if expected_count else 1.0
    precision = hit_unique / predicted_count if predicted_count else 1.0

    return CaseMetrics(
        case_name=expected.get("set_name", "unknown"),
        has_expected_conflict=expected_count > 0,
        expected_count=expected_count,
        predicted_count=predicted_count,
        hit_count=hit_unique,
        false_positive_count=false_positive,
        recall=recall,
        precision=precision,
    )


@dataclass
class SuiteReport:
    """一整轮(一次采样)在所有 case 上的汇总。分开报,不合成单一准确率。"""

    conflict_case_recall: float          # 有矛盾组的平均召回率(该抓的抓到没)
    conflict_case_count: int
    clean_case_false_positives: int      # clean 组的总误报数(对抗告警疲劳的硬指标)
    clean_case_count: int
    overall_precision: float             # 所有报出矛盾里对的比例
    cases: list[CaseMetrics] = field(default_factory=list)


def evaluate_suite(pairs: list[tuple[dict, dict]]) -> SuiteReport:
    """pairs: [(expected, prediction), ...] 覆盖所有 case,算一轮汇总。"""
    metrics = [evaluate_case(exp, pred) for exp, pred in pairs]
    conflict_cases = [m for m in metrics if m.has_expected_conflict]
    clean_cases = [m for m in metrics if not m.has_expected_conflict]

    conflict_recall = (
        sum(m.recall for m in conflict_cases) / len(conflict_cases)
        if conflict_cases else 1.0
    )
    clean_fp = sum(m.false_positive_count for m in clean_cases)
    total_hit = sum(m.hit_count for m in metrics)
    total_pred = sum(m.predicted_count for m in metrics)
    overall_precision = total_hit / total_pred if total_pred else 1.0

    return SuiteReport(
        conflict_case_recall=conflict_recall,
        conflict_case_count=len(conflict_cases),
        clean_case_false_positives=clean_fp,
        clean_case_count=len(clean_cases),
        overall_precision=overall_precision,
        cases=metrics,
    )


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _stdev(xs: list[float]) -> float:
    """总体标准差。样本太少(1个)时返回 0。"""
    if len(xs) < 2:
        return 0.0
    m = _mean(xs)
    return (sum((x - m) ** 2 for x in xs) / len(xs)) ** 0.5


def aggregate(reports: list[SuiteReport]) -> dict:
    """把 temp=0 跑 N 轮的多份汇总,算均值±方差(Q4)。

    方差大 = 判定不稳,说明该先改 prompt,而不是相信某一次的漂亮数字。
    """
    recalls = [r.conflict_case_recall for r in reports]
    fps = [float(r.clean_case_false_positives) for r in reports]
    precisions = [r.overall_precision for r in reports]
    return {
        "runs": len(reports),
        "recall_mean": _mean(recalls),
        "recall_stdev": _stdev(recalls),
        "clean_false_positive_mean": _mean(fps),
        "clean_false_positive_stdev": _stdev(fps),
        "precision_mean": _mean(precisions),
        "precision_stdev": _stdev(precisions),
    }
