from __future__ import annotations

from typing import Any
from uuid import uuid4

import httpx

from app.core.config import Settings


class LlmGatewayClient:
    """The sole model adapter used by Governance workflows."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

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
    ) -> dict[str, Any]:
        headers = {
            "X-Api-Key": self._settings.llm_gateway_api_key,
            "X-Tenant-Id": tenant_id,
            "X-User-Id": user_id,
            "X-Request-Id": f"gov_{uuid4().hex}",
            "X-Agent-Id": "agent-governance",
            "X-Agent-Version": "1.0",
            "X-Purpose": purpose,
        }
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0,
            "max_tokens": max_tokens,
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
