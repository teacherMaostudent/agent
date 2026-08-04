FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /service

RUN groupadd --system governance \
    && useradd --system --gid governance --home-dir /service governance

COPY platform-infra /platform-infra
COPY agent-governance/pyproject.toml agent-governance/README.md ./
COPY agent-governance/app ./app
COPY agent-governance/db ./db

RUN pip install /platform-infra . \
    && mkdir -p /data \
    && chown -R governance:governance /service /data

USER governance

ENV GOVERNANCE_DATABASE_PATH=/data/governance.db
EXPOSE 8081

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8081/health/ready', timeout=2)"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8081"]
