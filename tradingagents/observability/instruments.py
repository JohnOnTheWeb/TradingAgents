import logging

_logger = logging.getLogger(__name__)
_enabled = False


def enable_auto_instrumentation() -> None:
    """Enable Bedrock / botocore / requests auto-instrumentation.

    The blog SDK bundles LangChain + Bedrock instrumentors; when unavailable we fall back
    to the vanilla OpenTelemetry instrumentors for botocore + requests, which still
    captures gen_ai.* attributes for Bedrock calls made via boto3.
    """
    global _enabled
    if _enabled:
        return

    installed = False

    try:
        from opensearch_genai_observability_sdk import instrumentation  # type: ignore[import-not-found]

        instrumentation.instrument_all()
        installed = True
    except ImportError:
        pass
    except Exception:
        _logger.exception("opensearch-genai-observability-sdk instrumentation failed.")

    if not installed:
        try:
            from opentelemetry.instrumentation.botocore import BotocoreInstrumentor

            BotocoreInstrumentor().instrument()
        except ImportError:
            _logger.debug("opentelemetry-instrumentation-botocore not installed.")
        except Exception:
            _logger.exception("Botocore instrumentation failed.")

        try:
            from opentelemetry.instrumentation.requests import RequestsInstrumentor

            RequestsInstrumentor().instrument()
        except ImportError:
            _logger.debug("opentelemetry-instrumentation-requests not installed.")
        except Exception:
            _logger.exception("Requests instrumentation failed.")

    _enabled = True
