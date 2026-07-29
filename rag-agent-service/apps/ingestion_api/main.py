from app.ingestion.container import IngestionContainer
from app.platform.web import create_service_app
from app.service_api.ingestion_api import router

container = IngestionContainer()
app = create_service_app("knowledge-ingestion-api", container, [router])
