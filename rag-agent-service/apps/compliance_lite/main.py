from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI

from app.lite_compliance.api import router
from app.lite_compliance.service import LiteComplianceService
from app.lite_compliance.store import LiteComplianceStore


class Container:
    def __init__(self) -> None:
        path = Path(os.getenv("COMPLIANCE_LITE_DATABASE_PATH", "data/compliance-lite.db"))
        self.store = LiteComplianceStore(path)
        self.service = LiteComplianceService(self.store)

    def close(self) -> None:
        self.store.close()


container = Container()
app = FastAPI(
    title="AissuriQ Compliance Lite",
    version="0.1.0",
    description="Single-process rule-first feasibility service; LLM is optional.",
)
app.state.container = container
app.include_router(router, prefix="/api/v1")


@app.get("/api/v1/health")
def health() -> dict:
    return {
        "status": "UP",
        "service": "aissuriq-compliance-lite",
        "required_services": 1,
        "llm_enabled": False,
    }
