"""Composition helpers for local observability pipelines."""

from pleiad.observability.processor import TraceProcessor
from pleiad.observability.service import TraceService
from pleiad.observability.tracer import Tracer


def create_tracer(service: TraceService) -> Tracer:
    """Build the local direct Runtime-scope-to-Trace pipeline."""
    return Tracer(TraceProcessor(service))
