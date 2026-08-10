FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /service

# RAG contributes only shared contracts/context clients during this transition;
# this image contains the actual Agent and Runtime execution implementation and
# never starts RAG query or ingestion processes.
COPY platform-infra /platform-infra
COPY rag-agent-service/pyproject.toml rag-agent-service/README.md ./rag-agent-service/
COPY rag-agent-service/app ./rag-agent-service/app
COPY rag-agent-service/apps ./rag-agent-service/apps
COPY agent-runtime/pyproject.toml ./
COPY agent-runtime/src ./src
RUN pip install --no-cache-dir /platform-infra ./rag-agent-service .

RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /service/data \
    && chown -R appuser:appuser /service
USER appuser

CMD ["uvicorn", "agent_runtime_service.main:app", "--host", "0.0.0.0", "--port", "8001"]
