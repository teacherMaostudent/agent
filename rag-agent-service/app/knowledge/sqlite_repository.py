from pathlib import Path

from app.knowledge.durable_repository import DurableRepository
from app.storage.sqlite_kv import SqliteKv


class SqliteRepository(DurableRepository):
    """Development-only durable repository."""

    def __init__(self, db_path: Path) -> None:
        """以 SQLite KV 构建开发用持久化仓储。"""
        super().__init__(SqliteKv(db_path))
