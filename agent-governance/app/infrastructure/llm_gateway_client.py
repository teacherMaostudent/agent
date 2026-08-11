from __future__ import annotations

from typing import Any
from uuid import uuid4

import httpx
from platform_infra.identity import build_workload_token_provider

from app.core.config import Settings


class LlmGatewayClient:
    """The sole model adapter used by Governance workflows."""

    def __init__(self, settings: Settings) -> None:
        """保存冻结的网关配置，并构建随请求签发的工作负载身份提供器。"""
        self._settings = settings
        self._workload_identity = build_workload_token_provider(settings)

    async def complete(
        self,
        *,
        tenant_id: str,
        user_id: str,
        model: str,
        system: str,
        user: str,
        purpose: str,
        max_tokens: int = 2_000,
        temperature: float = 0,
        top_p: float = 1,
        response_schema: dict[str, Any] | None = None,
        model_revision: str = "",
        route_version: str = "",
    ) -> dict[str, Any]:
        """通过受控 Gateway 调用冻结模型路由，携带工作负载身份与可审计版本字段。"""
        headers = {
            "X-Api-Key": self._settings.llm_gateway_api_key,
            "X-Tenant-Id": tenant_id,
            "X-User-Id": user_id,
            "X-Request-Id": f"gov_{uuid4().hex}",
            "X-Agent-Id": "agent-governance",
            "X-Agent-Version": "1.0",
            "X-Purpose": purpose,
            "X-Model-Revision": model_revision,
            "X-Model-Route-Version": route_version,
        }
        headers.update(self._workload_identity.authorization_header())
        payload = {
            "model": model,
            "route_version": route_version,
            "model_revision": model_revision,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "top_p": top_p,
            "max_tokens": max_tokens,
        }
        if response_schema:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "governance_judge_v1",
                    "strict": True,
                    "schema": response_schema,
                },
            }
        async with httpx.AsyncClient(
            base_url=self._settings.llm_gateway_base_url,
            timeout=self._settings.llm_gateway_timeout_seconds,
        ) as client:
            response = await client.post("/v1/chat/completions", headers=headers, json=payload)
            response.raise_for_status()
            body = response.json()
        content = body["choices"][0]["message"]["content"]
        return {"content": content, "raw": body}
