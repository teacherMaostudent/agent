FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /service

# BFF 只依赖共享身份基础设施和 Runtime 的 HTTP 契约；禁止复制任一业务服务实现。
COPY platform-infra /platform-infra
COPY agent-web-bff/pyproject.toml ./
COPY agent-web-bff/src ./src
# 静态页面不参与 Python 依赖解析。将它放在安装层之后，UI 调整不会触发整套依赖重装。
RUN --mount=type=cache,target=/root/.cache/pip pip install /platform-infra .
COPY agent-web/public ./static

RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /service
USER appuser
CMD ["uvicorn", "agent_web_bff.main:app", "--host", "0.0.0.0", "--port", "8010"]
