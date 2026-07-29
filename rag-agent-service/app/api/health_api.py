from fastapi import APIRouter, HTTPException

from app.services import container

router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict:
    return {"status": "UP", "service": "rag-agent-service"}


@router.get("/health/ready")
def readiness() -> dict:
    """供部署探针检查 RAG 服务及其 LLM 网关依赖。"""
    if not container.settings.llm_enabled:
        return {
            "status": "UP",
            "llm": "DISABLED",
            "message": "LLM 未启用，条款覆盖结果将标记为 UNCERTAIN",
        }
    try:
        container.llm_gateway.healthcheck()
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"llm-gateway unavailable: {type(exc).__name__}",
        ) from exc
    return {
        "status": "UP",
        "llm": "READY",
        "gateway": container.settings.llm_gateway_base_url,
        "model": container.settings.llm_model,
    }
