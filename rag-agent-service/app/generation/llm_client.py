"""逆向生成客户端；底层统一调用 llm-gateway。"""

from app.infrastructure.llm_gateway_client import LlmGatewayClient


class LlmChatClient:
    def __init__(
        self,
        gateway: LlmGatewayClient,
        model: str,
    ) -> None:
        self.gateway = gateway
        self.model = model

    def complete(self, system_prompt: str, user_prompt: str, temperature: float = 0.3) -> str:
        """返回模型生成的纯文本内容。"""
        return self.gateway.complete(
            model=self.model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=temperature,
        )
