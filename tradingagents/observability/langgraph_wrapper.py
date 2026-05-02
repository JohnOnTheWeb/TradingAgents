from functools import wraps
from typing import Any, Callable

from tradingagents.observability.attributes import TA_AGENT_NODE
from tradingagents.observability.tracing import get_tracer


def wrap_node(name: str, fn: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap a LangGraph node function in a child span named ``ta.agent_node``."""

    @wraps(fn)
    def _wrapped(*args, **kwargs):
        tracer = get_tracer("tradingagents.graph")
        with tracer.start_as_current_span("ta.agent_node") as span:
            span.set_attribute(TA_AGENT_NODE, name)
            return fn(*args, **kwargs)

    return _wrapped
