FROM python:3.12-slim AS service-base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /service
COPY platform-infra /platform-infra
COPY platform-sdk /platform-sdk
COPY rag-agent-service/pyproject.toml rag-agent-service/README.md ./
COPY rag-agent-service/app ./app
COPY rag-agent-service/apps ./apps
# 与 Runtime 一样复用构建机 cache，不把 pip cache 带到运行时镜像中。
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install /platform-infra /platform-sdk .

RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /service/data \
    && chown -R appuser:appuser /service
USER appuser

# Each target is an independently versioned deployment artifact.  They share
# only the RAG application package and SDK, never the Runtime application.
FROM service-base AS context-service
ENV PLATFORM_ASGI_APP=apps.agent_context_service.main:app \
    PLATFORM_ASGI_PORT=8002
CMD ["python", "-m", "platform_infra.asgi_runner"]

FROM service-base AS rag-query-api
ENV PLATFORM_ASGI_APP=apps.rag_query_api.main:app \
    PLATFORM_ASGI_PORT=8003
CMD ["python", "-m", "platform_infra.asgi_runner"]

FROM service-base AS ingestion-api
ENV PLATFORM_ASGI_APP=apps.ingestion_api.main:app \
    PLATFORM_ASGI_PORT=8004
CMD ["python", "-m", "platform_infra.asgi_runner"]

FROM service-base AS ingestion-worker
CMD ["ingestion-temporal-worker"]
