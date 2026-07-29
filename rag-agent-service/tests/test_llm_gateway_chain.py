import pytest
from pydantic import ValidationError

from app.infrastructure.llm_gateway_client import LlmGatewayClient
from app.knowledge.repository import InMemoryRepository
from app.review.llm_judge import LlmJudge


def test_gateway_client_sends_identity_key_and_logical_model(monkeypatch) -> None:
    captured = {}

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "ok"}}]}

    class _Client:
        def __init__(self, timeout):
            captured["timeout"] = timeout

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def post(self, url, json, headers):
            captured.update(url=url, payload=json, headers=headers)
            return _Response()

    monkeypatch.setattr("app.infrastructure.llm_gateway_client.httpx.Client", _Client)
    gateway = LlmGatewayClient(
        "http://llm-gateway:8080",
        api_key="gateway-key",
        user_id="rag-agent-service",
    )
    gateway.chat_completion({"model": "qwen-plus", "messages": []})

    assert captured["url"] == "http://llm-gateway:8080/v1/chat/completions"
    assert captured["payload"]["model"] == "qwen-plus"
    assert captured["headers"]["X-Api-Key"] == "gateway-key"
    assert captured["headers"]["X-User-Id"] == "rag-agent-service"
    assert captured["headers"]["X-Request-Id"].startswith("rag-")


def test_gateway_client_forwards_execution_context(monkeypatch) -> None:
    captured = {}

    class _Response:
        def raise_for_status(self): pass
        def json(self): return {"choices": [{"message": {"content": "ok"}}]}

    class _Client:
        def __init__(self, timeout): pass
        def __enter__(self): return self
        def __exit__(self, *args): return None
        def post(self, url, json, headers):
            captured.update(headers=headers)
            return _Response()

    monkeypatch.setattr("app.infrastructure.llm_gateway_client.httpx.Client", _Client)
    LlmGatewayClient("http://gateway").chat_completion(
        {"model": "test", "messages": []},
        execution_headers={"X-Trace-Id": "trace-1", "X-Run-Id": "run-1", "X-Snapshot-Id": "snapshot-1"},
    )
    assert captured["headers"]["X-Trace-Id"] == "trace-1"
    assert captured["headers"]["X-Run-Id"] == "run-1"


def test_batch_judge_validates_gateway_response() -> None:
    repository = InMemoryRepository()
    item = repository.checklist["REQ-DOC-001"]

    class _Gateway:
        def complete_json(self, model, system_prompt, user_prompt):
            assert model == "deepseek-v4-flash"
            return {
                "results": [{
                    "requirementId": item.requirement_id,
                    "status": "PARTIAL",
                    "evidence": "文件有版本号",
                    "missingFields": ["批准人"],
                    "reason": "缺少批准信息",
                }]
            }

    result = LlmJudge(_Gateway(), "deepseek-v4-flash").judge_coverage_batch([(item, ["文件有版本号"])])
    assert result[item.requirement_id]["status"] == "PARTIAL"
    assert result[item.requirement_id]["missingFields"] == ["批准人"]


def test_batch_judge_rejects_unknown_status() -> None:
    item = InMemoryRepository().checklist["REQ-DOC-001"]

    class _Gateway:
        def complete_json(self, model, system_prompt, user_prompt):
            return {"results": [{"requirementId": item.requirement_id, "status": "MAYBE"}]}

    with pytest.raises(ValidationError):
        LlmJudge(_Gateway(), "deepseek-v4-flash").judge_coverage_batch([(item, ["正文"])])
