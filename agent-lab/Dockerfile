FROM python:3.12-slim
WORKDIR /service
COPY platform-infra /platform-infra
COPY platform-sdk /platform-sdk
COPY agent-lab/pyproject.toml ./
RUN pip install --no-cache-dir /platform-infra /platform-sdk .
COPY agent-lab/app ./app
RUN useradd --create-home --uid 10006 appuser \
    && mkdir -p /service/data \
    && chown -R appuser:appuser /service
USER appuser
EXPOSE 8092
ENV PLATFORM_ASGI_APP=app.main:app \
    PLATFORM_ASGI_PORT=8092
CMD ["python", "-m", "platform_infra.asgi_runner"]
