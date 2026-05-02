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
            pass

        sigv4_exporter = _build_sigv4_exporter(endpoint)
        if sigv4_exporter is not None:
            return sigv4_exporter
        _logger.warning(
            "TA_OTEL_SIGV4 is set but neither the blog SDK nor boto3 is available; "
            "falling back to unsigned OTLP HTTP exporter — requests will be rejected by OSIS."
        )

    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        return OTLPSpanExporter(endpoint=endpoint.rstrip("/") + "/v1/traces")
    except ImportError:
        _logger.warning(
            "opentelemetry-exporter-otlp-proto-http is not installed; tracing disabled."
        )
        return None


def _build_sigv4_exporter(endpoint: str):
    """OTLPSpanExporter subclass that SigV4-signs POSTs to OSIS (osis service).

    OSIS ingest endpoints require SigV4 against service ``osis``. The vendored
    OTLP-HTTP exporter uses a ``requests.Session``; we post-process the
    prepared request with ``botocore.auth.SigV4Auth`` before sending.
    """
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    except ImportError:
        return None

    try:
        import boto3
        from botocore.auth import SigV4Auth
        from botocore.awsrequest import AWSRequest
    except ImportError:
        return None

    region = (
        os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or "us-east-1"
    )
    session = boto3.Session()
    credentials = session.get_credentials()
    if credentials is None:
        _logger.warning("No AWS credentials available for SigV4 OTLP exporter.")
        return None
    signer = SigV4Auth(credentials, "osis", region)

    traces_url = endpoint.rstrip("/") + "/v1/traces"

    class _SigV4OTLPSpanExporter(OTLPSpanExporter):  # type: ignore[misc]
        def _export(self, serialized_data):  # type: ignore[override]
            aws_req = AWSRequest(
                method="POST",
                url=traces_url,
                data=serialized_data,
                headers={"Content-Type": "application/x-protobuf"},
            )
            signer.add_auth(aws_req)
            return self._session.post(
                url=traces_url,
                data=serialized_data,
                headers=dict(aws_req.headers),
                timeout=self._timeout,
            )

    return _SigV4OTLPSpanExporter(endpoint=traces_url)


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
