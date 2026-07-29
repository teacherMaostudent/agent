from app.platform.web import create_service_app
from app.rag.container import RagQueryContainer
from app.service_api.rag_query_api import router

container = RagQueryContainer()
app = create_service_app(
    "rag-query-api",
    container,
    [router],
    readiness=lambda: {"documents": len(container.repository.documents)},
)
