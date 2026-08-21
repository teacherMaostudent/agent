FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /service
COPY platform-infra /platform-infra
COPY platform-contracts /platform-contracts
COPY tool-gateway/pyproject.toml tool-gateway/README.md ./
COPY tool-gateway/app ./app
COPY tool-gateway/config ./config
RUN python -m pip install --no-cache-dir /platform-infra .

RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /service/data \
    && chown -R appuser:appuser /service
USER appuser

EXPOSE 8090
ENV PLATFORM_ASGI_APP=app.main:app \
    PLATFORM_ASGI_PORT=8090
CMD ["python", "-m", "platform_infra.asgi_runner"]
