from app.context.container import AgentContextContainer
from app.platform.web import create_service_app
from app.service_api.context_api import router

container = AgentContextContainer()
app = create_service_app("agent-context-service", container, [router])
