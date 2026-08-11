from app.core.config import Settings
from app.knowledge.postgres_repository import PostgresRepository
from app.knowledge.repository import InMemoryRepository
from app.knowledge.sqlite_repository import SqliteRepository


def build_repository(settings: Settings):
    """按持久化配置创建文档事实仓储，令查询/摄取服务不依赖具体数据库实现。"""
    if settings.persistence == "sqlite":
        return SqliteRepository(settings.sqlite_path)
    if settings.persistence == "postgres":
        return PostgresRepository(settings.database_url, settings.database_schema)
    return InMemoryRepository()
