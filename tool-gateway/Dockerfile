FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /service
COPY pyproject.toml README.md ./
COPY app ./app
COPY config ./config
RUN python -m pip install --no-cache-dir .

RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /service/data \
    && chown -R appuser:appuser /service
USER appuser

EXPOSE 8090
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8090"]
