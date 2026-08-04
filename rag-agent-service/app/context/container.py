from platform_infra.identity import build_workload_token_provider

from app.context.service import AgentContextService
from app.context.store import ConversationStore
from app.core.config import get_settings
from app.rag.client import HttpRagQueryClient
from app.storage.postgres_kv import PostgresKv
from app.storage.sqlite_kv import SqliteKv


class AgentContextContainer:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.workload_identity = build_workload_token_provider(self.settings)
        backend = None
        if self.settings.persistence == "sqlite":
            backend = SqliteKv(self.settings.sqlite_path)
        elif self.settings.persistence == "postgres":
            backend = PostgresKv(
                self.settings.database_url, self.settings.database_schema
            )
        self.store = ConversationStore(
            backend,
            retention_days=self.settings.context_retention_days,
            max_messages=self.settings.context_max_stored_messages,
        )
        self.rag_client = HttpRagQueryClient(
            self.settings.rag_query_base_url,
            self.settings.internal_service_api_key,
            self.settings.service_http_timeout,
            self.workload_identity,
        )
        self.context_service = AgentContextService(
            self.store,
            self.rag_client,
            self.settings.context_max_messages,
            self.settings.context_token_budget,
            self.settings.context_message_budget_ratio,
        )

    def close(self) -> None:
        self.store.close()
