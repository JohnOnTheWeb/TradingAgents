import atexit
import logging
import os
import threading

_logger = logging.getLogger(__name__)
_lock = threading.Lock()
_initialized = False
_tracer_provider = None


def _noop_tracer():
    from opentelemetry import trace

    return trace.get_tracer("tradingagents.noop")


def init_tracing(service_name: str) -> None:
    """Initialize OTLP tracing exporter. No-op when OTEL_EXPORTER_OTLP_ENDPOINT is unset.

    Idempotent: safe to call multiple times (e.g., from import + explicit call).
    """
    global _initialized, _tracer_provider

    with _lock:
        if _initialized:
            return

        endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
        if not endpoint:
            _initialized = True
            return

        try:
            from opentelemetry import trace
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor
        except ImportError:
            _logger.warning(
                "OTEL_EXPORTER_OTLP_ENDPOINT is set but opentelemetry-sdk is not installed; "
                "install with pip install '.[otel]'. Tracing disabled."
            )
            _initialized = True
            return

        exporter = _build_exporter(endpoint)
        if exporter is None:
            _initialized = True
            return

        resource = Resource.create(
            {
                "service.name": os.environ.get("OTEL_SERVICE_NAME", service_name),
            }
        )
        provider = TracerProvider(resource=resource)
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
        _tracer_provider = provider

        atexit.register(_shutdown)

        try:
            from tradingagents.observability import instruments

            instruments.enable_auto_instrumentation()
        except Exception:
            _logger.exception("Auto-instrumentation failed; continuing with manual spans only.")

        _initialized = True


def _build_exporter(endpoint: str):
    sigv4 = os.environ.get("TA_OTEL_SIGV4", "").lower() in ("1", "true", "yes")

    if sigv4:
        try:
            from opensearch_genai_observability_sdk.exporters import (  # type: ignore[import-not-found]
                AWSSigV4OTLPExporter,
            )

            return AWSSigV4OTLPExporter(endpoint=endpoint)
        except ImportError:
            _logger.warning(
                "TA_OTEL_SIGV4 is set but opensearch-genai-observability-sdk is not available; "
                "falling back to unsigned OTLP HTTP exporter."
            )

    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        return OTLPSpanExporter(endpoint=endpoint.rstrip("/") + "/v1/traces")
    except ImportError:
        _logger.warning(
            "opentelemetry-exporter-otlp-proto-http is not installed; tracing disabled."
        )
        return None


def _shutdown() -> None:
    if _tracer_provider is not None:
        try:
            _tracer_provider.shutdown()
        except Exception:
            _logger.exception("Tracer provider shutdown failed.")


def get_tracer(name: str = "tradingagents"):
    try:
        from opentelemetry import trace

        return trace.get_tracer(name)
    except ImportError:
        return _noop_tracer()
