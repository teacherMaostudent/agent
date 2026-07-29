from app.evaluation.coverage_eval import evaluate_coverage


def test_coverage_evaluation_tracks_false_missing_and_missed_defects() -> None:
    golden = {
        "REQ-1": "COVERED",
        "REQ-2": "MISSING",
        "REQ-3": "PARTIAL",
        "REQ-4": "NOT_APPLICABLE",
    }
    predicted = {
        "REQ-1": "MISSING",
        "REQ-2": "COVERED",
        "REQ-3": "UNCERTAIN",
        "REQ-4": "NOT_APPLICABLE",
    }
    result = evaluate_coverage(golden, predicted)
    assert result.total == 4
    assert result.false_missing == 1
    assert result.missed_defects == 1
    assert result.uncertain == 1
    assert result.accuracy == 0.25
