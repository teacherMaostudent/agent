FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /service
COPY platform-infra /platform-infra
# Tool Gateway consumes the shared desktop connector contract at runtime. Keep the
# dependency explicit in the image instead of relying on the source-tree PYTHONPATH
# used by tests, otherwise the production container fails during module import.
COPY platform-sdk /platform-sdk
COPY platform-contracts /platform-contracts
COPY tool-gateway/pyproject.toml tool-gateway/README.md ./
COPY tool-gateway/app ./app
COPY tool-gateway/config ./config
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install /platform-infra /platform-sdk .

RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /service/data \
    && chown -R appuser:appuser /service
USER appuser

EXPOSE 8090
ENV PLATFORM_ASGI_APP=app.main:app \
    PLATFORM_ASGI_PORT=8090
CMD ["python", "-m", "platform_infra.asgi_runner"]
