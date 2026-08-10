from app.platform.web import create_service_app

from agent_runtime_service.runtime.container import AgentRuntimeContainer
from agent_runtime_service.service_api.runtime_api import router

container = AgentRuntimeContainer()
app = create_service_app("agent-runtime", container, [router])
