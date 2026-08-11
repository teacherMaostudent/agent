from __future__ import annotations

from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor


def configure_telemetry(
    app: FastAPI,
    *,
    enabled: bool,
    service_name: str,
    environment: str,
    endpoint: str,
) -> None:
    """按服务/环境创建 OTLP Trace Provider 并装配 FastAPI、HTTPX 自动埋点。

    关闭时完全不修改全局 provider，便于本地测试；生产端点必须由部署层提供，避免
    应用代码把追踪数据发送到未经批准的地址。
    """
    if not enabled:
        return
    provider = TracerProvider(
        resource=Resource.create(
            {"service.name": service_name, "deployment.environment": environment}
        )
    )
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    trace.set_tracer_provider(provider)
    FastAPIInstrumentor.instrument_app(app, tracer_provider=provider)
    HTTPXClientInstrumentor().instrument(tracer_provider=provider)
