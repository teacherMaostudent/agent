FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /service

RUN groupadd --system controlplane \
    && useradd --system --gid controlplane --home-dir /service controlplane

COPY platform-infra /platform-infra
COPY agent-control-plane/pyproject.toml agent-control-plane/README.md ./
COPY agent-control-plane/app ./app
COPY agent-control-plane/db ./db

RUN pip install /platform-infra .

RUN mkdir -p /data \
    && chown -R controlplane:controlplane /service /data

USER controlplane

ENV CONTROL_PLANE_DATABASE_PATH=/data/control-plane.db
ENV PLATFORM_ASGI_APP=app.main:app \
    PLATFORM_ASGI_PORT=8080 \
    PLATFORM_HEALTHCHECK_URL=http://127.0.0.1:8080/health/ready
EXPOSE 8080

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python", "-m", "platform_infra.healthcheck"]

CMD ["python", "-m", "platform_infra.asgi_runner"]
