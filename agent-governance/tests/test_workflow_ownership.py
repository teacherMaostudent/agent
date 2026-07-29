from __future__ import annotations

import json
from typing import Any

from fastapi.testclient import TestClient


class FakeGateway:
    async def complete(self, **kwargs: Any) -> dict[str, Any]:
        if kwargs["purpose"] == "compliance-review":
            content = json.dumps(
                {
                    "riskLevel": "HIGH",
                    "summary": "Deviation investigation is missing.",
                    "defects": [],
                    "capa": {
                        "correctiveAction": "Open a deviation.",
                        "preventiveAction": "Add release control.",
                        "ownerRole": "QA",
                        "dueDays": 7,
                        "verificationMethod": "Review next batches.",
                    },
                    "needHumanReview": True,
                }
            )
        else:
            content = json.dumps(
                {
                    "dimensionScores": {
                        "correctness": 90,
                        "faithfulness": 90,
                        "relevance": 90,
                        "safetyCompliance": 90,
                    },
                    "overallScore": 90,
                    "passed": True,
                    "reason": "supported",
                    "unsupportedClaims": [],
                    "evidence": ["reference"],
                }
            )
        return {
            "content": content,
            "raw": {"id": "gateway-response", "gateway": {"costEstimated": 0.01}},
        }


def test_evaluation_assets_regression_judge_and_gate_are_governance_owned(
    client: TestClient, auditor_headers: dict[str, str]
) -> None:
    container = client.app.state.container
    container.evaluation._gateway = FakeGateway()

    golden = client.put(
        "/v1/governance/evaluations/golden-dataset",
        headers=auditor_headers,
        json={
            "id": "case-1",
            "question": "What is required?",
            "groundTruth": "Approval is required.",
            "contexts": ["Approval is required before release."],
            "tags": ["release"],
        },
    )
    assert golden.status_code == 200, golden.text

    regression = client.post(
        "/v1/governance/evaluations/regression-runs",
        headers=auditor_headers,
        json={
            "promptVersionId": "p1",
            "retrievalStrategyId": "r1",
            "answerByQuestion": {"What is required?": "Approval is required."},
        },
    )
    assert regression.status_code == 200
    assert regression.json()["summary"]["answerSimilarity"] == 1.0

    judge = client.post(
        "/v1/governance/evaluations/judge-runs",
        headers=auditor_headers,
        json={
            "candidateModel": "candidate",
            "caseIds": ["case-1"],
            "candidateAnswers": {"What is required?": "Approval is required."},
        },
    )
    assert judge.status_code == 200, judge.text
    assert judge.json()["metrics"]["averageScore"] == 90.0

    gate = client.post(
        f"/v1/governance/evaluations/judge-runs/{judge.json()['id']}/quality-gate",
        headers=auditor_headers,
    )
    assert gate.status_code == 200
    assert gate.json()["passed"] is True


def test_compliance_review_and_human_confirmation_live_in_governance(
    client: TestClient, auditor_headers: dict[str, str]
) -> None:
    container = client.app.state.container
    container.compliance._gateway = FakeGateway()

    review = client.post(
        "/v1/governance/compliance/reviews",
        headers=auditor_headers,
        json={
            "businessId": "batch-1",
            "documentType": "batch-record",
            "content": "Excursion occurred without deviation.",
            "model": "review-model",
        },
    )
    assert review.status_code == 200, review.text
    assert review.json()["status"] == "PENDING_HUMAN_REVIEW"

    confirmed = client.post(
        f"/v1/governance/compliance/reviews/{review.json()['reviewId']}/confirm",
        headers=auditor_headers,
        json={"reviewer": "qa@example.com", "decision": "CONFIRM"},
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "CONFIRMED"
    assert confirmed.json()["confirmedBy"] == "qa@example.com"

    audit = client.get(
        "/v1/governance/compliance/audit-logs", headers=auditor_headers
    )
    assert {item["action"] for item in audit.json()} == {
        "AI_REVIEW_CREATED",
        "HUMAN_CONFIRMED",
    }
