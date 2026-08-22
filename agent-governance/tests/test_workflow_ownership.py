from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi.testclient import TestClient

from app.application.evaluation_service import GOLDEN_CANDIDATE


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


def test_golden_candidate_queue_exposes_minimal_projection_and_promotes_only_on_approval(
    client: TestClient, auditor_headers: dict[str, str]
) -> None:
    """候选审核队列不能借 Console 接口返回线上样本原始请求/响应。"""
    repository = client.app.state.container.repository
    asyncio.run(
        repository.upsert_document(
            "tenant-a",
            GOLDEN_CANDIDATE,
            "candidate-a",
            {
                "id": "candidate-a",
                "sampleId": "sample-a",
                "question": "What is the release rule?",
                "groundTruth": "Approval is required.",
                "contexts": ["secret evidence body"],
                "tags": ["release"],
                "status": "PENDING",
                "createdAt": "2026-01-01T00:00:00Z",
                "updatedAt": "2026-01-01T00:00:00Z",
            },
        )
    )

    queue = client.get(
        "/v1/governance/evaluations/online/golden-candidates", headers=auditor_headers
    )
    assert queue.status_code == 200, queue.text
    assert queue.json()["items"][0]["id"] == "candidate-a"
    assert "contexts" not in queue.json()["items"][0]

    reviewed = client.post(
        "/v1/governance/evaluations/online/golden-candidates/candidate-a/review",
        headers=auditor_headers,
        json={"approved": True, "note": "expert confirmed"},
    )
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["status"] == "PUBLISHED"


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
            "tags": ["release", "red-team", "prompt-injection"],
            "expertLabels": {"passed": True},
            "labelerId": "expert-1",
            "reviewStatus": "APPROVED",
            "criticality": "critical",
            "expectedEvidenceIds": ["doc-approval"],
        },
    )
    assert golden.status_code == 200, golden.text

    prompt = client.put(
        "/v1/governance/evaluations/prompt-versions",
        headers=auditor_headers,
        json={
            "id": "p1",
            "version": "1.0.0",
            "system": "Evaluate only against the supplied evidence and return JSON.",
        },
    )
    assert prompt.status_code == 200, prompt.text

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
            "retrievedEvidenceByCase": {"case-1": [{"id": "doc-approval"}]},
        },
    )
    assert judge.status_code == 200, judge.text
    assert judge.json()["metrics"]["averageScore"] == 90.0
    assert judge.json()["metrics"]["retrieval"]["recallAtK"] == 1.0
    snapshot_id = judge.json()["evaluationSnapshotId"]
    snapshot = next(
        item
        for item in client.get("/v1/governance/evaluations", headers=auditor_headers).json()[
            "executionSnapshots"
        ]
        if item["id"] == snapshot_id
    )
    assert len(snapshot["contentHash"]) == 64
    assert snapshot["sampling"] == {"temperature": 0, "topP": 1, "maxTokens": 2000}
    assert snapshot["outputSchemaVersion"] == "governance-judge-output/v1"
    assert snapshot["assets"]["goldenCases"][0]["groundTruth"] == "Approval is required."

    calibration = client.post(
        f"/v1/governance/evaluations/judge-runs/{judge.json()['id']}/calibration",
        headers=auditor_headers,
    )
    assert calibration.status_code == 200, calibration.text
    assert calibration.json()["passed"] is True

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

    audit = client.get("/v1/governance/compliance/audit-logs", headers=auditor_headers)
    assert {item["action"] for item in audit.json()} == {
        "AI_REVIEW_CREATED",
        "HUMAN_CONFIRMED",
    }
