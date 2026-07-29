from app.platform.web import create_service_app
from app.runtime.container import AgentRuntimeContainer
from app.service_api.runtime_api import router

container = AgentRuntimeContainer()
app = create_service_app("agent-runtime", container, [router])
