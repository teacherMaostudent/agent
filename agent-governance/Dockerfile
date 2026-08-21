FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /service

RUN groupadd --system governance \
    && useradd --system --gid governance --home-dir /service governance

COPY platform-infra /platform-infra
COPY platform-contracts /platform-contracts
COPY agent-governance/pyproject.toml agent-governance/README.md ./
COPY agent-governance/app ./app
COPY agent-governance/db ./db

RUN pip install /platform-infra . \
    && mkdir -p /data \
    && chown -R governance:governance /service /data

USER governance

ENV GOVERNANCE_DATABASE_PATH=/data/governance.db
ENV PLATFORM_ASGI_APP=app.main:app \
    PLATFORM_ASGI_PORT=8081 \
    PLATFORM_HEALTHCHECK_URL=http://127.0.0.1:8081/health/ready
EXPOSE 8081

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python", "-m", "platform_infra.healthcheck"]

CMD ["python", "-m", "platform_infra.asgi_runner"]
