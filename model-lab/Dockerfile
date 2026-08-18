FROM python:3.12-slim
WORKDIR /service
COPY platform-infra /platform-infra
COPY model-lab/pyproject.toml ./
COPY model-lab/app ./app
RUN pip install --no-cache-dir /platform-infra .
EXPOSE 8091
ENV PLATFORM_ASGI_APP=app.main:app \
    PLATFORM_ASGI_PORT=8091
CMD ["python", "-m", "platform_infra.asgi_runner"]
