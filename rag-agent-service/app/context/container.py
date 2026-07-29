from app.context.service import AgentContextService
from app.context.store import ConversationStore
from app.core.config import get_settings
from app.rag.client import HttpRagQueryClient


class AgentContextContainer:
    def __init__(self) -> None:
        self.settings = get_settings()
        sqlite_path = self.settings.sqlite_path if self.settings.persistence == "sqlite" else None
        self.store = ConversationStore(sqlite_path)
        self.rag_client = HttpRagQueryClient(
            self.settings.rag_query_base_url,
            self.settings.internal_service_api_key,
            self.settings.service_http_timeout,
        )
        self.context_service = AgentContextService(
            self.store,
            self.rag_client,
            self.settings.context_max_messages,
            self.settings.context_token_budget,
        )

    def close(self) -> None:
        self.store.close()
