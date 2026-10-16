import asyncio
import collections.abc

import pytest
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from pleiad.observability.config import ObservabilityConfig
from pleiad.observability.tracing import Observability


def make_observability(exporter: InMemorySpanExporter) -> Observability:
    """Create an isolated OTel provider for deterministic span assertions."""
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return Observability(config=ObservabilityConfig(), tracer_provider=provider)


def test_nested_spans_use_standard_parent_context_and_safe_attributes() -> None:
    exporter = InMemorySpanExporter()
    observability = make_observability(exporter)

    with observability.span("pleiad.run", attributes={"pleiad.run.id": "run-1"}) as run_span:
        with observability.span("pleiad.model.generate", attributes={"prompt": "secret"}) as model_span:
            observability.finish(model_span, "succeeded")
        observability.finish(run_span, "succeeded")

    spans = {span.name: span for span in exporter.get_finished_spans()}
    assert spans["pleiad.model.generate"].context.trace_id == spans["pleiad.run"].context.trace_id
    assert spans["pleiad.model.generate"].parent is not None
    assert spans["pleiad.model.generate"].parent.span_id == spans["pleiad.run"].context.span_id
    model_attributes = spans["pleiad.model.generate"].attributes
    assert model_attributes is not None
    assert "prompt" not in model_attributes


def test_default_observability_ignores_the_host_global_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Omitting observability does not consult or emit through host-global providers."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr("opentelemetry.trace.get_tracer_provider", lambda: provider)

    observability = Observability()
    with observability.span("pleiad.run") as span:
        observability.finish(span, "succeeded")

    assert exporter.get_finished_spans() == ()


def test_concurrent_tasks_keep_their_otel_contexts_separate() -> None:
    exporter = InMemorySpanExporter()
    observability = make_observability(exporter)

    async def execute(root_name: str) -> None:
        with observability.span(root_name) as root_span:
            with observability.span(f"{root_name}.child") as child_span:
                observability.finish(child_span, "succeeded")
            observability.finish(root_span, "succeeded")

    async def scenario() -> None:
        await asyncio.gather(execute("root-a"), execute("root-b"))

    asyncio.run(scenario())

    spans = exporter.get_finished_spans()
    traces = {span.name.split(".")[0]: span.context.trace_id for span in spans}
    assert traces["root-a"] != traces["root-b"]


def test_exporter_failure_does_not_escape_the_observed_operation() -> None:
    class FailingExporter(SpanExporter):
        def export(self, spans: collections.abc.Sequence[ReadableSpan]) -> SpanExportResult:
            raise RuntimeError("export failed")

        def shutdown(self) -> None:
            return None

        def force_flush(self, timeout_millis: int = 30_000) -> bool:
            return True

    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(FailingExporter()))
    observability = Observability(tracer_provider=provider)

    with observability.span("pleiad.run") as span:
        observability.finish(span, "succeeded")


def test_span_creation_failure_does_not_block_business_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    """A tracer failure before span entry leaves the business result unchanged."""

    class FailingTracer:
        def start_as_current_span(self, name: str, **options: object) -> object:
            raise RuntimeError("synthetic tracer failure")

    observability = Observability()
    monkeypatch.setattr(observability, "tracer", FailingTracer())

    with observability.span("pleiad.run"):
        result = "business-result"

    assert result == "business-result"


def test_span_close_failure_does_not_replace_business_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """A close failure cannot replace the original exception from observed code."""

    class FailingManager:
        def __enter__(self) -> object:
            return object()

        def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> bool:
            raise RuntimeError("synthetic close failure")

    class FailingTracer:
        def start_as_current_span(self, name: str, **options: object) -> FailingManager:
            return FailingManager()

    observability = Observability()
    monkeypatch.setattr(observability, "tracer", FailingTracer())

    with pytest.raises(ValueError, match="business failure"), observability.span("pleiad.run"):
        raise ValueError("business failure")
