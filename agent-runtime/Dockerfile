FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /service

# Runtime receives Context, RAG and Tool capabilities only over authenticated
# network contracts.  It must not install another service's application code.
COPY platform-infra /platform-infra
COPY platform-sdk /platform-sdk
COPY agent-runtime/pyproject.toml ./
COPY agent-runtime/src ./src
RUN pip install --no-cache-dir /platform-infra /platform-sdk .

RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /service/data \
    && chown -R appuser:appuser /service
USER appuser

ENV PLATFORM_ASGI_APP=agent_runtime_service.main:app \
    PLATFORM_ASGI_PORT=8001
CMD ["python", "-m", "platform_infra.asgi_runner"]
