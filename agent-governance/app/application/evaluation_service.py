"""Evaluation assets, quality gates and privacy-preserving runtime traces.

Governance owns evaluation decisions and retention.  The LLM Gateway reports
execution facts but never decides whether a release passes a quality gate.
"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from typing import Any
from uuid import uuid4

from app.application.exceptions import NotFoundError
from app.core.config import Settings
from app.domain.data_protection import classify_payload, protect_payload, sampled
from app.infrastructure.llm_gateway_client import LlmGatewayClient
from app.infrastructure.sqlite_repository import SqliteRepository

PROMPT_VERSION = "eval-prompt-version"
RETRIEVAL_STRATEGY = "eval-retrieval-strategy"
GOLDEN_CASE = "eval-golden-case"
REGRESSION_RUN = "eval-regression-run"
PHOENIX_TRACE = "eval-phoenix-trace"
JUDGE_RUBRIC = "eval-judge-rubric"
JUDGE_RUN = "eval-judge-run"
QUALITY_GATE = "eval-quality-gate"
ONLINE_SAMPLE = "eval-online-sample"
GOLDEN_CANDIDATE = "eval-golden-candidate"
EVALUATION_SNAPSHOT = "eval-execution-snapshot"

JUDGE_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "dimensionScores",
        "overallScore",
        "passed",
        "reason",
        "unsupportedClaims",
        "evidence",
    ],
    "properties": {
        "dimensionScores": {"type": "object", "additionalProperties": {"type": "integer"}},
        "overallScore": {"type": "integer", "minimum": 0, "maximum": 100},
        "passed": {"type": "boolean"},
        "reason": {"type": "string"},
        "unsupportedClaims": {"type": "array", "items": {"type": "string"}},
        "evidence": {"type": "array", "items": {"type": "string"}},
    },
}
DEFAULT_JUDGE_PROMPT = (
    "You are an independent evaluator. Return only JSON matching the supplied schema. "
    "Score only from the question, reference answer, context and candidate answer."
)


def _now() -> str:
    """Internal helper for module; preserve its caller-facing invariant."""
    return datetime.now(UTC).isoformat()


def _id(value: object | None = None) -> str:
    """Internal helper for module; preserve its caller-facing invariant."""
    return str(value).strip() if value is not None and str(value).strip() else uuid4().hex


class EvaluationService:
    """Owns evaluation assets and workflows; Gateway is only the model executor."""

    def __init__(
        self,
        repository: SqliteRepository,
        settings: Settings,
        gateway: LlmGatewayClient,
    ) -> None:
        """Initialize EvaluationService dependencies and local state."""
        self._repository = repository
        self._settings = settings
        self._gateway = gateway

    async def snapshot(self, tenant_id: str) -> dict[str, Any]:
        """Perform snapshot within the EvaluationService ownership boundary."""
        return {
            "store": "governance",
            "promptVersions": await self._repository.list_documents(tenant_id, PROMPT_VERSION),
            "retrievalStrategies": await self._repository.list_documents(
                tenant_id, RETRIEVAL_STRATEGY
            ),
            "goldenDataset": await self._repository.list_documents(tenant_id, GOLDEN_CASE),
            "regressionRuns": await self._repository.list_documents(tenant_id, REGRESSION_RUN),
            "phoenixTraces": (await self._repository.list_documents(tenant_id, PHOENIX_TRACE, 100)),
            "judgeRubrics": await self._repository.list_documents(tenant_id, JUDGE_RUBRIC),
            "judgeRuns": await self._repository.list_documents(tenant_id, JUDGE_RUN),
            "qualityGates": await self._repository.list_documents(tenant_id, QUALITY_GATE),
            "executionSnapshots": await self._repository.list_documents(
                tenant_id, EVALUATION_SNAPSHOT
            ),
        }

    async def upsert_asset(
        self, tenant_id: str, kind: str, request: dict[str, Any]
    ) -> dict[str, Any]:
        """Persist state while preserving the transaction and audit boundary."""
        saved = dict(request)
        saved["id"] = _id(saved.get("id"))
        saved["createdAt"] = _now()
        if kind == JUDGE_RUBRIC:
            saved.setdefault("passScore", 75)
            saved.setdefault("dimensions", [])
        return await self._repository.upsert_document(tenant_id, kind, saved["id"], saved)

    async def record_trace(self, tenant_id: str, request: dict[str, Any]) -> dict[str, Any]:
        """Run the bounded record trace operation and surface failures."""
        saved = dict(request)
        saved["traceId"] = _id(saved.get("traceId"))
        saved["timestamp"] = _now()
        return await self._repository.upsert_document(
            tenant_id, PHOENIX_TRACE, saved["traceId"], saved
        )

    async def record_gateway_trace(self, tenant_id: str, request: dict[str, Any]) -> dict[str, Any]:
        """Persist an eligible trace only after applying capture and retention policy."""
        request_id = _id(request.get("requestId"))
        success = bool(request.get("success"))
        if success and not sampled(request_id, self._settings.online_trace_sample_rate):
            return {"id": request_id, "status": "NOT_SAMPLED"}
        # Purging before insert keeps the retention guarantee true even when
        # the producer is high-volume and no background janitor is running.
        cutoff = (
            datetime.now(UTC)
            - timedelta(days=self._settings.online_trace_retention_days)
        ).isoformat()
        await self._repository.purge_documents_before(
            tenant_id, [PHOENIX_TRACE, ONLINE_SAMPLE], cutoff
        )
        protected_request = protect_payload(
            request.get("request") or {},
            capture_content=self._settings.capture_prompt_response_content,
        )
        protected_response = protect_payload(
            request.get("response") or {},
            capture_content=self._settings.capture_prompt_response_content,
        )
        classification = classify_payload(
            capture_content=self._settings.capture_prompt_response_content,
            protected={"request": protected_request, "response": protected_response},
        )
        await self.record_trace(
            tenant_id,
            {
                "traceId": request.get("traceId"),
                "requestId": request.get("requestId"),
                "spanName": "llm-gateway.chat",
                "input": json.dumps(protected_request, ensure_ascii=False),
                "output": json.dumps(protected_response, ensure_ascii=False),
                "metadata": {
                    "success": request.get("success"),
                    "latencyMs": request.get("latencyMs"),
                    "cost": request.get("cost"),
                    "currency": request.get("currency"),
                    "dataClassification": classification,
                    "retentionDays": self._settings.online_trace_retention_days,
                },
            },
        )
        sample_id = request_id
        existing = await self._repository.get_document(tenant_id, ONLINE_SAMPLE, sample_id)
        sample = {
            **(existing or {}),
            "id": sample_id,
            "requestId": sample_id,
            "traceId": request.get("traceId"),
            "tenantId": tenant_id,
            "userId": request.get("userId"),
            "model": request.get("requestedModel"),
            "request": protected_request,
            "response": protected_response,
            "success": bool(request.get("success")),
            "latencyMs": request.get("latencyMs", 0),
            "cost": request.get("cost", 0),
            "currency": request.get("currency", ""),
            "status": "CAPTURED",
            "dataClassification": classification,
            "retentionDays": self._settings.online_trace_retention_days,
            "disposition": "SAMPLE_POOL" if request.get("success") else "HUMAN_REVIEW",
            "updatedAt": _now(),
            "createdAt": (existing or {}).get("createdAt", _now()),
        }
        return await self._repository.upsert_document(tenant_id, ONLINE_SAMPLE, sample_id, sample)

    async def record_feedback(
        self, tenant_id: str, user_id: str, request: dict[str, Any]
    ) -> dict[str, Any]:
        """Run the bounded record feedback operation and surface failures."""
        sample_id = _id(request.get("requestId"))
        sample = await self._repository.get_document(tenant_id, ONLINE_SAMPLE, sample_id) or {
            "id": sample_id,
            "requestId": sample_id,
            "tenantId": tenant_id,
            "request": {},
            "response": {},
            "createdAt": _now(),
        }
        rating = int(request.get("rating") or 0)
        sample.update(
            {
                "userFeedback": {
                    **request,
                    "userId": user_id,
                    "recordedAt": _now(),
                },
                "status": "FEEDBACK_RECORDED",
                "disposition": "HUMAN_REVIEW" if rating <= 2 else "SAMPLE_POOL",
                "updatedAt": _now(),
            }
        )
        return await self._repository.upsert_document(tenant_id, ONLINE_SAMPLE, sample_id, sample)

    async def online_snapshot(self, tenant_id: str) -> dict[str, Any]:
        """Perform online snapshot within the EvaluationService ownership boundary."""
        samples = await self._repository.list_documents(tenant_id, ONLINE_SAMPLE)
        candidates = await self._repository.list_documents(tenant_id, GOLDEN_CANDIDATE)
        return {
            "store": "governance",
            "samples": samples,
            "humanReviewQueue": [
                item for item in samples if item.get("disposition") == "HUMAN_REVIEW"
            ],
            "samplePool": [item for item in samples if item.get("disposition") == "SAMPLE_POOL"],
            "goldenCandidates": candidates,
        }

    async def judge_online(self, tenant_id: str, user_id: str, sample_id: str) -> dict[str, Any]:
        """Perform judge online within the EvaluationService ownership boundary."""
        sample = await self._repository.get_document(tenant_id, ONLINE_SAMPLE, sample_id)
        if not sample:
            raise NotFoundError(f"Unknown online sample: {sample_id}")
        question = _question(sample.get("request") or {})
        answer = _answer(sample.get("response") or {})
        feedback = sample.get("userFeedback") or {}
        case = {
            "id": sample_id,
            "question": question,
            "groundTruth": feedback.get("expectedAnswer", ""),
            "contexts": [],
        }
        rubric = await self._rubric(tenant_id, "default")
        primary = await self._judge_once(
            tenant_id,
            user_id,
            self._settings.judge_primary_model,
            "primary",
            case,
            answer or "[NO_RESPONSE_FROM_CANDIDATE]",
            rubric,
        )
        secondary = await self._judge_once(
            tenant_id,
            user_id,
            self._settings.judge_secondary_model,
            "secondary",
            case,
            answer or "[NO_RESPONSE_FROM_CANDIDATE]",
            rubric,
        )
        verdict = _consensus(primary, secondary, rubric)
        sample.update(
            {
                "judgeResult": {
                    "primaryVerdict": primary,
                    "secondaryVerdict": secondary,
                    "finalVerdict": verdict,
                },
                "status": "JUDGED",
                "disposition": "SAMPLE_POOL" if verdict["passed"] else "HUMAN_REVIEW",
                "updatedAt": _now(),
            }
        )
        return await self._repository.upsert_document(tenant_id, ONLINE_SAMPLE, sample_id, sample)

    async def review_online_sample(
        self,
        tenant_id: str,
        user_id: str,
        sample_id: str,
        request: dict[str, Any],
    ) -> dict[str, Any]:
        """Perform review online sample within the EvaluationService ownership boundary."""
        sample = await self._repository.get_document(tenant_id, ONLINE_SAMPLE, sample_id)
        if not sample:
            raise NotFoundError(f"Unknown online sample: {sample_id}")
        sample.update(
            {
                "humanReview": {**request, "reviewedBy": user_id, "reviewedAt": _now()},
                "status": "HUMAN_REVIEWED",
                "disposition": "SAMPLE_POOL",
                "updatedAt": _now(),
            }
        )
        decision = str(request.get("decision") or "").upper()
        if decision in {"CONFIRMED_FAILURE", "GOLDEN_CANDIDATE"}:
            candidate = {
                "id": uuid4().hex,
                "sampleId": sample_id,
                "question": _question(sample.get("request") or {}),
                "groundTruth": request.get("expectedAnswer")
                or (sample.get("userFeedback") or {}).get("expectedAnswer", ""),
                "contexts": request.get("contexts") or [],
                "tags": request.get("tags") or ["online"],
                "status": "PENDING",
                "createdAt": _now(),
                "updatedAt": _now(),
            }
            await self._repository.upsert_document(
                tenant_id, GOLDEN_CANDIDATE, candidate["id"], candidate
            )
        return await self._repository.upsert_document(tenant_id, ONLINE_SAMPLE, sample_id, sample)

    async def review_golden_candidate(
        self,
        tenant_id: str,
        user_id: str,
        candidate_id: str,
        request: dict[str, Any],
    ) -> dict[str, Any]:
        """Perform review golden candidate within the EvaluationService ownership boundary."""
        candidate = await self._repository.get_document(tenant_id, GOLDEN_CANDIDATE, candidate_id)
        if not candidate:
            raise NotFoundError(f"Unknown Golden candidate: {candidate_id}")
        approved = bool(request.get("approved"))
        candidate.update(
            {
                "status": "PUBLISHED" if approved else "REJECTED",
                "reviewedBy": user_id,
                "reviewedAt": _now(),
                "reviewNote": request.get("note", ""),
                "updatedAt": _now(),
            }
        )
        if approved:
            await self.upsert_asset(
                tenant_id,
                GOLDEN_CASE,
                {
                    "id": request.get("goldenCaseId") or candidate["id"],
                    "question": candidate["question"],
                    "groundTruth": request.get("groundTruth") or candidate.get("groundTruth", ""),
                    "contexts": candidate.get("contexts") or [],
                    "tags": candidate.get("tags") or ["online"],
                },
            )
        return await self._repository.upsert_document(
            tenant_id, GOLDEN_CANDIDATE, candidate_id, candidate
        )

    async def run_regression(self, tenant_id: str, request: dict[str, Any]) -> dict[str, Any]:
        """Run the bounded run regression operation and surface failures."""
        answers = request.get("answerByQuestion") or {}
        cases = await self._repository.list_documents(tenant_id, GOLDEN_CASE)
        execution_snapshot = await self._compile_snapshot(
            tenant_id, request, cases, await self._rubric(tenant_id, request.get("rubricId"))
        )
        cases = execution_snapshot["assets"]["goldenCases"]
        scores = []
        for case in cases:
            answer = str(answers.get(case.get("question"), ""))
            similarity = _similarity(answer, str(case.get("groundTruth", "")))
            contexts = case.get("contexts") or []
            recall = max((_similarity(answer, str(item)) for item in contexts), default=0.0)
            scores.append(
                {
                    "caseId": case["id"],
                    "answerSimilarity": similarity,
                    "contextRecall": recall,
                    "faithfulness": min(1.0, max(similarity, recall)) if answer else 0.0,
                }
            )
        run = {
            "id": uuid4().hex,
            "timestamp": _now(),
            "promptVersionId": request.get("promptVersionId"),
            "retrievalStrategyId": request.get("retrievalStrategyId"),
            "evaluationSnapshotId": execution_snapshot["id"],
            "evaluationSnapshotHash": execution_snapshot["contentHash"],
            "scores": scores,
            "summary": {
                key: _average(item[key] for item in scores)
                for key in ("answerSimilarity", "contextRecall", "faithfulness")
            },
        }
        return await self._repository.upsert_document(tenant_id, REGRESSION_RUN, run["id"], run)

    async def judge(
        self,
        tenant_id: str,
        user_id: str,
        request: dict[str, Any],
    ) -> dict[str, Any]:
        """Perform judge within the EvaluationService ownership boundary."""
        rubric = await self._rubric(tenant_id, request.get("rubricId"))
        all_cases = await self._repository.list_documents(tenant_id, GOLDEN_CASE)
        selected = set(request.get("caseIds") or [])
        cases = [item for item in all_cases if not selected or item["id"] in selected]
        if not cases:
            raise NotFoundError("No Golden cases matched the Judge request.")
        execution_snapshot = await self._compile_snapshot(tenant_id, request, cases, rubric)
        assets = execution_snapshot["assets"]
        cases = assets["goldenCases"]
        rubric = assets["rubric"]

        results = []
        for case in cases:
            answer = (request.get("candidateAnswers") or {}).get(case.get("question"))
            if not answer:
                answer = await self._candidate_answer(
                    tenant_id,
                    user_id,
                    execution_snapshot["models"]["candidate"],
                    assets["prompt"],
                    case,
                )
            primary = await self._judge_once(
                tenant_id,
                user_id,
                execution_snapshot["models"]["primary"],
                "primary",
                case,
                answer,
                rubric,
                assets["prompt"],
            )
            secondary = await self._judge_once(
                tenant_id,
                user_id,
                execution_snapshot["models"]["secondary"],
                "secondary",
                case,
                answer,
                rubric,
                assets["prompt"],
            )
            disagreement = abs(primary["overallScore"] - secondary["overallScore"])
            arbitrator = None
            if disagreement > self._settings.judge_disagreement_threshold:
                arbitrator = await self._judge_once(
                    tenant_id,
                    user_id,
                    execution_snapshot["models"]["arbitrator"],
                    "arbitrator",
                    case,
                    answer,
                    rubric,
                    assets["prompt"],
                    prior=[primary, secondary],
                )
            final = arbitrator or _consensus(primary, secondary, rubric)
            results.append(
                {
                    "caseId": case["id"],
                    "question": case.get("question", ""),
                    "candidateAnswer": answer,
                    "primaryVerdict": primary,
                    "secondaryVerdict": secondary,
                    "arbitratorVerdict": arbitrator,
                    "finalVerdict": final,
                    "arbitrated": arbitrator is not None,
                    "disagreement": disagreement,
                }
            )

        final_scores = [item["finalVerdict"]["overallScore"] for item in results]
        failed = sum(not item["finalVerdict"]["passed"] for item in results)
        arbitrated = sum(item["arbitrated"] for item in results)
        run = {
            "id": uuid4().hex,
            "timestamp": _now(),
            "candidateModel": request.get("candidateModel"),
            "rubricId": rubric["id"],
            "promptVersionId": request.get("promptVersionId"),
            "retrievalStrategyId": request.get("retrievalStrategyId"),
            "evaluationSnapshotId": execution_snapshot["id"],
            "evaluationSnapshotHash": execution_snapshot["contentHash"],
            "cases": results,
            "metrics": {
                "averageScore": _average(final_scores),
                "passRate": (len(results) - failed) / len(results),
                "arbitrationRate": arbitrated / len(results),
                "failedCases": failed,
            },
            "status": "COMPLETED",
            "metadata": request.get("metadata") or {},
        }
        return await self._repository.upsert_document(tenant_id, JUDGE_RUN, run["id"], run)

    async def _compile_snapshot(
        self,
        tenant_id: str,
        request: dict[str, Any],
        cases: list[dict[str, Any]],
        rubric: dict[str, Any],
    ) -> dict[str, Any]:
        """Freeze every variable that can change an evaluation result before execution."""
        prompt = await self._prompt(tenant_id, request.get("promptVersionId"))
        snapshot = {
            "id": f"evalsnap_{uuid4().hex}",
            "createdAt": _now(),
            "assets": {
                "prompt": deepcopy(prompt),
                "rubric": deepcopy(rubric),
                "goldenCases": deepcopy(cases),
                "retrievalStrategyId": request.get("retrievalStrategyId"),
            },
            "models": {
                "primary": self._model_spec("primary"),
                "secondary": self._model_spec("secondary"),
                "arbitrator": self._model_spec("arbitrator"),
                "candidate": {
                    "model": str(
                        request.get("candidateModel") or self._settings.judge_primary_model
                    ),
                    "revision": str(request.get("candidateModelRevision") or "caller-declared"),
                },
            },
            "sampling": {
                "temperature": self._settings.judge_temperature,
                "topP": self._settings.judge_top_p,
                "maxTokens": self._settings.judge_max_tokens,
            },
            "outputSchema": deepcopy(JUDGE_OUTPUT_SCHEMA),
            "outputSchemaVersion": "governance-judge-output/v1",
            "routeVersion": self._settings.judge_model_route_version,
        }
        snapshot["contentHash"] = _hash(
            {key: value for key, value in snapshot.items() if key != "id"}
        )
        return await self._repository.upsert_document(
            tenant_id, EVALUATION_SNAPSHOT, snapshot["id"], snapshot
        )

    async def _prompt(self, tenant_id: str, prompt_id: object) -> dict[str, Any]:
        """Resolve a versioned prompt; never silently read a mutable prompt at run time."""
        if prompt_id:
            prompt = await self._repository.get_document(tenant_id, PROMPT_VERSION, str(prompt_id))
            if not prompt:
                raise NotFoundError(f"Unknown prompt version: {prompt_id}")
            if not isinstance(prompt.get("system"), str) or not prompt["system"].strip():
                raise ValueError("Prompt version must contain a non-empty system field")
            return prompt
        return {"id": "governance-judge-v1", "version": "1.0.0", "system": DEFAULT_JUDGE_PROMPT}

    def _model_spec(self, role: str) -> dict[str, str]:
        """Bind an evaluator role to a configured model revision and route release."""
        return {
            "model": str(getattr(self._settings, f"judge_{role}_model")),
            "revision": str(getattr(self._settings, f"judge_{role}_model_revision")),
        }

    async def quality_gate(
        self, tenant_id: str, run_id: str, request: dict[str, Any] | None
    ) -> dict[str, Any]:
        """Perform quality gate within the EvaluationService ownership boundary."""
        run = await self._repository.get_document(tenant_id, JUDGE_RUN, run_id)
        if not run:
            raise NotFoundError(f"Unknown judge run: {run_id}")
        overrides = request or {}
        limits = {
            "minimumAverageScore": float(
                overrides.get("minAverageScore", self._settings.quality_gate_min_average_score)
            ),
            "minimumPassRate": float(
                overrides.get("minPassRate", self._settings.quality_gate_min_pass_rate)
            ),
            "maximumArbitrationRate": float(
                overrides.get(
                    "maxArbitrationRate",
                    self._settings.quality_gate_max_arbitration_rate,
                )
            ),
            "maximumFailedCases": int(
                overrides.get("maxFailedCases", self._settings.quality_gate_max_failed_cases)
            ),
        }
        metrics = run["metrics"]
        reasons = []
        if float(metrics["averageScore"]) < limits["minimumAverageScore"]:
            reasons.append("averageScore below minimum")
        if float(metrics["passRate"]) < limits["minimumPassRate"]:
            reasons.append("passRate below minimum")
        if float(metrics["arbitrationRate"]) > limits["maximumArbitrationRate"]:
            reasons.append("arbitrationRate above maximum")
        if int(metrics["failedCases"]) > limits["maximumFailedCases"]:
            reasons.append("failedCases above maximum")
        result = {
            "id": uuid4().hex,
            "runId": run_id,
            "timestamp": _now(),
            "passed": not reasons,
            "exitCode": 0 if not reasons else 1,
            "metrics": {**metrics, **limits},
            "reasons": reasons,
        }
        return await self._repository.upsert_document(tenant_id, QUALITY_GATE, result["id"], result)

    async def _rubric(self, tenant_id: str, rubric_id: object) -> dict[str, Any]:
        """Internal helper for EvaluationService; preserve its caller-facing invariant."""
        resolved = str(rubric_id or "default")
        rubric = await self._repository.get_document(tenant_id, JUDGE_RUBRIC, resolved)
        if rubric:
            return rubric
        if resolved != "default":
            raise NotFoundError(f"Unknown judge rubric: {resolved}")
        return await self.upsert_asset(
            tenant_id,
            JUDGE_RUBRIC,
            {
                "id": "default",
                "name": "通用 RAG 与 Agent 回答质量量表",
                "version": "1.0.0",
                "instructions": "只根据问题、参考答案和上下文评分。",
                "passScore": 75,
                "dimensions": [
                    {
                        "name": "correctness",
                        "description": "事实和结论是否正确",
                        "weight": 0.35,
                        "minimumScore": 70,
                    },
                    {
                        "name": "faithfulness",
                        "description": "主张是否有证据支持",
                        "weight": 0.30,
                        "minimumScore": 75,
                    },
                    {
                        "name": "relevance",
                        "description": "是否直接回答问题",
                        "weight": 0.20,
                        "minimumScore": 65,
                    },
                    {
                        "name": "safetyCompliance",
                        "description": "是否遵守安全合规边界",
                        "weight": 0.15,
                        "minimumScore": 80,
                    },
                ],
            },
        )

    async def _candidate_answer(
        self,
        tenant_id: str,
        user_id: str,
        model_spec: dict[str, str],
        prompt: dict[str, Any],
        case: dict[str, Any],
    ) -> str:
        """Internal helper for EvaluationService; preserve its caller-facing invariant."""
        response = await self._gateway.complete(
            tenant_id=tenant_id,
            user_id=user_id,
            model=model_spec["model"],
            system=str(prompt["system"]),
            user=json.dumps(case, ensure_ascii=False),
            purpose="evaluation-candidate",
            max_tokens=self._settings.judge_max_tokens,
            temperature=self._settings.judge_temperature,
            top_p=self._settings.judge_top_p,
            model_revision=model_spec["revision"],
            route_version=self._settings.judge_model_route_version,
        )
        return str(response["content"])

    async def _judge_once(
        self,
        tenant_id: str,
        user_id: str,
        model: str | dict[str, str],
        role: str,
        case: dict[str, Any],
        answer: str,
        rubric: dict[str, Any],
        prompt: dict[str, Any] | None = None,
        prior: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Internal helper for EvaluationService; preserve its caller-facing invariant."""
        payload = {
            "question": case.get("question"),
            "groundTruth": case.get("groundTruth"),
            "contexts": case.get("contexts") or [],
            "candidateAnswer": answer,
            "rubric": rubric,
            "priorJudgments": prior,
        }
        model_spec = model if isinstance(model, dict) else {"model": model, "revision": "legacy"}
        response = await self._gateway.complete(
            tenant_id=tenant_id,
            user_id=user_id,
            model=model_spec["model"],
            system=str((prompt or {}).get("system") or DEFAULT_JUDGE_PROMPT),
            user=json.dumps(payload, ensure_ascii=False),
            purpose=f"evaluation-judge-{role}",
            max_tokens=self._settings.judge_max_tokens,
            temperature=self._settings.judge_temperature,
            top_p=self._settings.judge_top_p,
            response_schema=JUDGE_OUTPUT_SCHEMA,
            model_revision=model_spec["revision"],
            route_version=self._settings.judge_model_route_version,
        )
        parsed = _validated_judge_output(str(response["content"]))
        scores = {
            item["name"]: int((parsed.get("dimensionScores") or {}).get(item["name"], 0))
            for item in rubric.get("dimensions", [])
        }
        overall = int(parsed.get("overallScore", _weighted_score(scores, rubric)))
        passed = overall >= int(rubric.get("passScore", 75)) and all(
            scores.get(item["name"], 0) >= int(item.get("minimumScore", 0))
            for item in rubric.get("dimensions", [])
        )
        raw = response["raw"]
        gateway = raw.get("gateway") or {}
        return {
            "judgeRole": role,
            "model": model_spec["model"],
            "modelRevision": model_spec["revision"],
            "dimensionScores": scores,
            "overallScore": overall,
            "passed": passed,
            "reason": str(parsed.get("reason", "")),
            "unsupportedClaims": parsed.get("unsupportedClaims") or [],
            "evidence": parsed.get("evidence") or [],
            "cost": gateway.get("costEstimated", 0),
            "currency": gateway.get("costCurrency", ""),
            "requestId": raw.get("id", ""),
            "rawOutput": response["content"],
        }


def _hash(value: object) -> str:
    """Hash canonical JSON so a run can prove precisely which inputs it consumed."""
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(serialized.encode()).hexdigest()


def _validated_judge_output(text: str) -> dict[str, Any]:
    """Fail closed when a provider ignores the requested strict JSON schema."""
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        raise ValueError("Judge response is not valid JSON") from None
    if not isinstance(value, dict) or set(value) != set(JUDGE_OUTPUT_SCHEMA["required"]):
        raise ValueError("Judge response does not match governance-judge-output/v1")
    if not isinstance(value["dimensionScores"], dict) or not all(
        isinstance(score, int) and 0 <= score <= 100
        for score in value["dimensionScores"].values()
    ):
        raise ValueError("Judge dimensionScores must be integer scores between 0 and 100")
    if not isinstance(value["overallScore"], int) or not 0 <= value["overallScore"] <= 100:
        raise ValueError("Judge overallScore must be an integer between 0 and 100")
    if not isinstance(value["passed"], bool) or not isinstance(value["reason"], str):
        raise ValueError("Judge passed and reason fields have invalid types")
    for field in ("unsupportedClaims", "evidence"):
        if not isinstance(value[field], list) or not all(
            isinstance(item, str) for item in value[field]
        ):
            raise ValueError(f"Judge {field} must be a string array")
    return value


def _tokens(value: str) -> set[str]:
    """Internal helper for module; preserve its caller-facing invariant."""
    return {item for item in re.split(r"[\W_]+", value.lower()) if item}


def _similarity(left: str, right: str) -> float:
    """Internal helper for module; preserve its caller-facing invariant."""
    left_tokens = _tokens(left)
    if not left_tokens or not right:
        return 0.0
    normalized = right.lower()
    return round(sum(token in normalized for token in left_tokens) / len(left_tokens), 4)


def _average(values: Any) -> float:
    """Internal helper for module; preserve its caller-facing invariant."""
    items = [float(item) for item in values]
    return round(sum(items) / len(items), 4) if items else 0.0


def _weighted_score(scores: dict[str, int], rubric: dict[str, Any]) -> int:
    """Internal helper for module; preserve its caller-facing invariant."""
    dimensions = rubric.get("dimensions") or []
    total = sum(Decimal(str(item.get("weight", 0))) for item in dimensions)
    if not total:
        return 0
    value = sum(
        Decimal(scores.get(item["name"], 0)) * Decimal(str(item.get("weight", 0)))
        for item in dimensions
    )
    return int((value / total).quantize(Decimal("1")))


def _consensus(
    left: dict[str, Any], right: dict[str, Any], rubric: dict[str, Any]
) -> dict[str, Any]:
    """Internal helper for module; preserve its caller-facing invariant."""
    scores = {
        item["name"]: round(
            (
                left["dimensionScores"].get(item["name"], 0)
                + right["dimensionScores"].get(item["name"], 0)
            )
            / 2
        )
        for item in rubric.get("dimensions", [])
    }
    overall = _weighted_score(scores, rubric)
    return {
        **left,
        "judgeRole": "consensus",
        "model": f"{left['model']}+{right['model']}",
        "dimensionScores": scores,
        "overallScore": overall,
        "passed": overall >= int(rubric.get("passScore", 75)),
        "reason": f"{left.get('reason', '')}; {right.get('reason', '')}".strip("; "),
        "unsupportedClaims": list(
            dict.fromkeys(left.get("unsupportedClaims", []) + right.get("unsupportedClaims", []))
        ),
        "evidence": list(dict.fromkeys(left.get("evidence", []) + right.get("evidence", []))),
        "cost": float(left.get("cost", 0)) + float(right.get("cost", 0)),
    }


def _question(request: dict[str, Any]) -> str:
    """Internal helper for module; preserve its caller-facing invariant."""
    messages = request.get("messages") or []
    for item in reversed(messages):
        if item.get("role") == "user":
            return str(item.get("content") or "")
    return ""


def _answer(response: dict[str, Any]) -> str:
    """Internal helper for module; preserve its caller-facing invariant."""
    choices = response.get("choices") or []
    if choices:
        return str((choices[0].get("message") or {}).get("content") or "")
    return ""
