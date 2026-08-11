from threading import Lock

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.resources import DEPLOYMENT_ENVIRONMENT, SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

_lock = Lock()
_configured = False


def configure_tracing(settings) -> None:
    """进程内只配置一次 OpenTelemetry，避免热重载重复导出相同 span。"""
    global _configured
    if not settings.otel_enabled or _configured:
        return
    with _lock:
        if _configured:
            return
        provider = TracerProvider(
            resource=Resource.create(
                {
                    SERVICE_NAME: settings.otel_service_name,
                    DEPLOYMENT_ENVIRONMENT: settings.otel_environment,
                }
            )
        )
        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.otel_exporter_endpoint))
        )
        trace.set_tracer_provider(provider)
        HTTPXClientInstrumentor().instrument()
        _configured = True


def instrument_fastapi(app, settings) -> None:
    """在启用追踪时为 FastAPI 加入请求 span；禁用时不引入额外中间件。"""
    if settings.otel_enabled:
        FastAPIInstrumentor.instrument_app(app)
