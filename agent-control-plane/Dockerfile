FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /service

RUN groupadd --system controlplane \
    && useradd --system --gid controlplane --home-dir /service controlplane

COPY pyproject.toml README.md ./
COPY app ./app
COPY db ./db

RUN pip install .

RUN mkdir -p /data \
    && chown -R controlplane:controlplane /service /data

USER controlplane

ENV CONTROL_PLANE_DATABASE_PATH=/data/control-plane.db
EXPOSE 8080

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health/ready', timeout=2)"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
