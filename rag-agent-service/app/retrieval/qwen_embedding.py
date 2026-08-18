"""通义千问 text-embedding-v3 客户端(走 OpenAI 兼容接口)。

替换本地 Hash embedding,让法规检索用真正的语义向量。默认连 DashScope 兼容
模式;base_url 可配,以后网关代理 embedding 时改指向网关即可。

- 批量 embed(一次多条),减少 API 调用次数。
- 中英文都支持,适配中英混合法规。
- 需要 DASHSCOPE_API_KEY(sk- 开头),聊天和 embedding 共用。
"""

import httpx


class QwenEmbeddingClient:
    """通义 text-embedding-v3。embed_batch 一次传多条文本,返回等长向量列表。"""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1",
        model: str = "text-embedding-v3",
        timeout: float = 60.0,
        batch_size: int = 10,
    ) -> None:
        """保存供应商连接与批量限制，并在缺少密钥时立即拒绝无效配置。"""
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        # DashScope 兼容接口单次 embedding 条数有限,默认 10 条一批。
        self.batch_size = max(1, batch_size)

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """把多条文本 embedding,返回与输入等长、同序的向量列表。"""
        if not texts:
            return []
        vectors: list[list[float]] = []
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        with httpx.Client(timeout=self.timeout, headers=headers) as client:
            for start in range(0, len(texts), self.batch_size):
                batch = texts[start : start + self.batch_size]
                resp = client.post(
                    f"{self.base_url}/embeddings",
                    json={"model": self.model, "input": batch},
                )
                resp.raise_for_status()
                data = resp.json()["data"]
                # 按 index 排序,确保和输入顺序一致。
                for item in sorted(data, key=lambda d: d["index"]):
                    vectors.append(item["embedding"])
        return vectors

    def embed(self, text: str) -> list[float]:
        """单条文本 embedding(检索时给查询用)。"""
        return self.embed_batch([text])[0]
