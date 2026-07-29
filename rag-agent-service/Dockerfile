FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /service
COPY pyproject.toml README.md ./
COPY app ./app
COPY apps ./apps
RUN pip install --no-cache-dir .

RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /service/data \
    && chown -R appuser:appuser /service
USER appuser

CMD ["uvicorn", "apps.agent_runtime.main:app", "--host", "0.0.0.0", "--port", "8001"]
