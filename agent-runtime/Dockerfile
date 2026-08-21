FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /service

# Runtime receives Context, RAG and Tool capabilities only over authenticated
# network contracts.  It must not install another service's application code.
COPY platform-infra /platform-infra
COPY platform-sdk /platform-sdk
COPY platform-contracts /platform-contracts
COPY agent-runtime/pyproject.toml ./
COPY agent-runtime/src ./src
# BuildKit 的 pip 缓存不会进入最终镜像；修改 Runtime 源码时可复用已下载的 wheels，
# 同时镜像仍由这条显式安装命令完整、可重复地构建。
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install /platform-infra /platform-sdk .

RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /service/data \
    && chown -R appuser:appuser /service
USER appuser

ENV PLATFORM_ASGI_APP=agent_runtime_service.main:app \
    PLATFORM_ASGI_PORT=8001 \
    RUNTIME_CONTRACTS_SCHEMA_DIR=/platform-contracts/schemas
CMD ["python", "-m", "platform_infra.asgi_runner"]
