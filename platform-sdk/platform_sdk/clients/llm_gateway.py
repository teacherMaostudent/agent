"""OpenAI-compatible LLM Gateway client shared by platform services."""

import json
import re
from contextvars import ContextVar
from typing import Any
from uuid import uuid4

import httpx
from opentelemetry import trace
from platform_infra.identity import WorkloadTokenProvider


class LlmGatewayClient:
    """llm-gateway 的 OpenAI-compatible 同步适配器。

    RAG 服务只认识网关逻辑模型名。厂家选择、密钥、路由、fallback、限额和
    成本审计全部留在 Java 网关，避免业务服务与任一厂家耦合。
    """

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        user_id: str = "rag-agent-service",
        timeout: float = 60.0,
        workload_identity: WorkloadTokenProvider | None = None,
        mtls: dict[str, Any] | None = None,
    ) -> None:
        """保存网关地址、服务身份与每次上下文独立的最近用量。

        SDK 只传逻辑模型名；供应商密钥、路由、限流及 fallback 必须留在 LLM Gateway，
        从而避免业务服务与模型厂商耦合。
        """
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key.strip()
        self.user_id = user_id.strip() or "rag-agent-service"
        self.timeout = timeout
        self.workload_identity = workload_identity
        self.mtls = mtls or {}
        self._last_usage: ContextVar[dict | None] = ContextVar(
            f"llm_gateway_usage_{id(self)}",
            default=None,
        )

    def chat_completion(
        self, payload: dict, *, execution_headers: dict[str, str] | None = None
    ) -> dict:
        """发送 OpenAI 兼容请求并记录 Gateway 返回的用量。

        ``execution_headers`` 只能由 Runtime 构造，用于追踪、预算和发布版本审计；
        调用者的任意外部 Header 不应直接透传。
        """
        headers = {
            "Content-Type": "application/json",
            "X-User-Id": self.user_id,
            "X-Request-Id": f"rag-{uuid4().hex}",
        }
        if self.api_key:
            headers["X-Api-Key"] = self.api_key
        if self.workload_identity is not None:
            headers.update(self.workload_identity.authorization_header())
        if execution_headers:
            headers.update(
                {key: value for key, value in execution_headers.items() if value}
            )
        with trace.get_tracer(__name__).start_as_current_span(
            "gateway.chat_completion"
        ) as span:
            span.set_attribute("gen_ai.request.model", str(payload.get("model", "")))
            with httpx.Client(timeout=self.timeout, **self.mtls) as client:
                response = client.post(
                    f"{self.base_url}/v1/chat/completions",
                    json=payload,
                    headers=headers,
                )
                response.raise_for_status()
                data = response.json()
                usage = data.get("usage", {}) if isinstance(data, dict) else {}
                gateway = data.get("gateway", {}) if isinstance(data, dict) else {}
                if isinstance(usage, dict) and isinstance(gateway, dict):
                    self._last_usage.set({**usage, "gateway": gateway})
                return data

    def last_cost_usd(self) -> float | None:
        """读取当前上下文最后一次调用的 USD 费用，拒绝未统一币种的账单。"""
        usage = self._last_usage.get() or {}
        gateway = usage.get("gateway", {})
        if gateway and gateway.get("costCurrency") != "USD":
            raise ValueError("llm-gateway cost currency must be USD")
        value = gateway.get("costEstimated", usage.get("cost_usd"))
        return float(value) if isinstance(value, (int, float)) else None

    def complete(
        self,
        model: str,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.0,
        json_response: bool = False,
        execution_headers: dict[str, str] | None = None,
    ) -> str:
        """执行单轮聊天补全；JSON 模式仅请求网关约束，不替代后续语义验证。"""
        payload: dict = {
            "model": model,
            "temperature": temperature,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        if json_response:
            payload["response_format"] = {"type": "json_object"}
        data = self.chat_completion(payload, execution_headers=execution_headers)
        return data["choices"][0]["message"]["content"]

    def complete_json(
        self,
        model: str,
        system_prompt: str,
        user_prompt: str,
        *,
        execution_headers: dict[str, str] | None = None,
    ) -> dict:
        """解析模型 JSON 输出并兼容常见 Markdown 代码围栏，非法 JSON 应向上失败。"""
        raw = self.complete(
            model,
            system_prompt,
            user_prompt,
            json_response=True,
            execution_headers=execution_headers,
        )
        text = (raw or "").strip()
        if text.startswith("```"):
            text = re.sub(r"^```[a-zA-Z]*\n", "", text)
            text = re.sub(r"\n```$", "", text).strip()
        return json.loads(text)

    def healthcheck(self) -> None:
        """探测网关健康状态，不消耗模型额度或创建审计运行。"""
        with httpx.Client(timeout=min(self.timeout, 5.0), **self.mtls) as client:
            response = client.get(f"{self.base_url}/actuator/health")
            response.raise_for_status()
