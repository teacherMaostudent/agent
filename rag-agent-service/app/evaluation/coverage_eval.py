from dataclasses import dataclass


@dataclass(frozen=True)
class CoverageEvaluation:
    total: int
    exact_matches: int
    false_missing: int
    missed_defects: int
    uncertain: int
    accuracy: float


def evaluate_coverage(golden: dict[str, str], predicted: dict[str, str]) -> CoverageEvaluation:
    """比较专家标注与模型结果；只评价双方都有的 requirement_id。"""
    common_ids = sorted(set(golden) & set(predicted))
    exact = sum(golden[item] == predicted[item] for item in common_ids)
    false_missing = sum(
        predicted[item] == "MISSING" and golden[item] in {"COVERED", "NOT_APPLICABLE"}
        for item in common_ids
    )
    missed_defects = sum(
        predicted[item] == "COVERED" and golden[item] in {"PARTIAL", "MISSING"}
        for item in common_ids
    )
    uncertain = sum(predicted[item] == "UNCERTAIN" for item in common_ids)
    total = len(common_ids)
    return CoverageEvaluation(
        total=total,
        exact_matches=exact,
        false_missing=false_missing,
        missed_defects=missed_defects,
        uncertain=uncertain,
        accuracy=exact / total if total else 0.0,
    )
