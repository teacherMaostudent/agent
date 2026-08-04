from app.core.config import Settings
from app.knowledge.repository import InMemoryRepository
from app.knowledge.sqlite_repository import SqliteRepository
from app.knowledge.postgres_repository import PostgresRepository


def build_repository(settings: Settings):
    """Build the metadata repository without coupling a service to AppContainer."""
    if settings.persistence == "sqlite":
        return SqliteRepository(settings.sqlite_path)
    if settings.persistence == "postgres":
        return PostgresRepository(settings.database_url, settings.database_schema)
    return InMemoryRepository()
