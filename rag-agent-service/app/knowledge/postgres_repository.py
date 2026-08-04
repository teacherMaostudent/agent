from app.knowledge.durable_repository import DurableRepository
from app.storage.postgres_kv import PostgresKv


class PostgresRepository(DurableRepository):
    def __init__(self, dsn: str, schema: str) -> None:
        super().__init__(PostgresKv(dsn, schema))
