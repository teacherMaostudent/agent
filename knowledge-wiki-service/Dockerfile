FROM python:3.12-slim
WORKDIR /service
COPY platform-infra /platform-infra
COPY platform-contracts /platform-contracts
COPY knowledge-wiki-service/pyproject.toml ./
# 先复制包源码再安装，使 console-script `knowledge-wiki-relay` 在镜像中拥有真实的
# `app.relay` 模块；API 不能因为工作目录碰巧可导入而掩盖错误的 Python 打包顺序。
COPY knowledge-wiki-service/app ./app
RUN pip install --no-cache-dir /platform-infra .
RUN useradd --create-home --uid 10010 appuser \
    && mkdir -p /service/data \
    && chown -R appuser:appuser /service
USER appuser
EXPOSE 8094
ENV PLATFORM_ASGI_APP=app.main:app PLATFORM_ASGI_PORT=8094 \
    KNOWLEDGE_WIKI_CONTRACTS_SCHEMA_DIR=/platform-contracts/schemas
CMD ["python", "-m", "platform_infra.asgi_runner"]
