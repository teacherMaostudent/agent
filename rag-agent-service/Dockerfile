FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /service
COPY platform-infra /platform-infra
COPY rag-agent-service/pyproject.toml rag-agent-service/README.md ./
COPY rag-agent-service/app ./app
COPY rag-agent-service/apps ./apps
RUN pip install --no-cache-dir /platform-infra .

RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /service/data \
    && chown -R appuser:appuser /service
USER appuser

CMD ["uvicorn", "apps.agent_runtime.main:app", "--host", "0.0.0.0", "--port", "8001"]
