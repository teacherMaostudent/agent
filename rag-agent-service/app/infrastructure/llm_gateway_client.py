"""Compatibility import path; LLM Gateway client now belongs to platform-sdk."""

from platform_sdk.clients.llm_gateway import LlmGatewayClient

__all__ = ["LlmGatewayClient"]
