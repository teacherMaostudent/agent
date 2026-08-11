from app.knowledge.durable_repository import DurableRepository
from app.storage.postgres_kv import PostgresKv


class PostgresRepository(DurableRepository):
    def __init__(self, dsn: str, schema: str) -> None:
        """以 PostgreSQL KV 构建多副本可共享的文档仓储。"""
        super().__init__(PostgresKv(dsn, schema))
