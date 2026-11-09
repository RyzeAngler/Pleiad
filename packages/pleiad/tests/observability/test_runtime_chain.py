import asyncio
from pathlib import Path

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import Histogram, InMemoryMetricReader, Sum
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from pleiad.contracts import JsonObject
from pleiad.models.openai.response import OpenAIUsage
from pleiad.observability.tracing import Observability
from pleiad.runtime.limits import RunLimits
from pleiad.sessions.backends.in_memory import InMemorySessionStore
from pleiad.sessions.input import InputEnvelope
from pleiad.sessions.session import Session
from pleiad.tools.base import ToolOutput
from pleiad.tools.contracts import ToolContext
from tests.fakes.runtime import (
    BlockingOpenAIModel,
    FakeAnthropicModel,
    FakeOpenAIModel,
    anthropic_text_batch,
    make_runner,
    text_batch,
    tool_batch,
)
from tests.runtime_support import run_agent


def make_runtime_observability(exporter: InMemorySpanExporter) -> Observability:
    """Create an isolated provider for the real Runner path."""
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return Observability(tracer_provider=provider)


def test_runner_emits_one_generation_in_the_real_run_step_model_chain(tmp_path: Path) -> None:
    model = FakeOpenAIModel((text_batch("done", "response-1"),))
    runner, agent = make_runner(model, tmp_path)
    exporter = InMemorySpanExporter()
    runner.observability = make_runtime_observability(exporter)
    runner.loop.observability = runner.observability

    result = asyncio.run(
        run_agent(runner, agent, "hello", session=Session("observed-run", InMemorySessionStore()), limits=RunLimits())
    )

    assert result.state.value == "succeeded"
    names = [span.name for span in exporter.get_finished_spans()]
    assert names.count("pleiad.run") == 1
    assert names.count("pleiad.step") == 1
    assert names.count("pleiad.model.generate") == 1
    assert "pleiad.tool.execute" not in names


def test_input_consumption_metrics_count_one_accepted_input_across_retries_and_multiple_steps(tmp_path: Path) -> None:
    async def scenario() -> None:
        async def echo(context: ToolContext, arguments: JsonObject) -> ToolOutput:
            return arguments

        reader = InMemoryMetricReader()
        provider = MeterProvider(metric_readers=[reader])
        runner, agent = make_runner(
            FakeOpenAIModel((tool_batch("call"), text_batch("finished", "response"))), tmp_path, echo
        )
        runner.observability = Observability(meter_provider=provider)
        runner.loop.observability = runner.observability
        session = Session("observed-input", InMemorySessionStore())
        envelope = InputEnvelope(input_id="request", kind="user", source="private-source", content="Use the tool")
        await session.enqueue(envelope)
        assert (await session.enqueue(envelope)).duplicate
        try:
            handle = await runner.start(
                agent,
                session=session,
                invocation_id="invoke",
                input_ids=(envelope.input_id,),
                input_mode="batch",
                limits=RunLimits(),
            )
            async with handle:
                result = await handle.result()
            assert result.state.value == "succeeded"
            assert len((await session.store.snapshot(session.session_id)).steps) == 2
            data = reader.get_metrics_data()
            assert data is not None
            metrics = {
                metric.name: metric.data
                for resource in data.resource_metrics
                for scope in resource.scope_metrics
                for metric in scope.metrics
            }
            consumed = metrics["pleiad.input.consumed"]
            assert isinstance(consumed, Sum)
            assert consumed.data_points[0].value == 1
            assert consumed.data_points[0].attributes == {"kind": "user"}
            latency = metrics["pleiad.input.consumption.latency"]
            assert isinstance(latency, Histogram)
            assert latency.data_points[0].count == 1
            assert latency.data_points[0].sum >= 0
        finally:
            await runner.aclose()
            provider.shutdown()

    asyncio.run(scenario())


def test_runner_places_tool_execution_beside_model_generations(tmp_path: Path) -> None:
    model = FakeOpenAIModel((tool_batch("call-1"), text_batch("finished", "response-2")))

    async def echo(context: ToolContext, arguments: JsonObject) -> ToolOutput:
        return arguments

    runner, agent = make_runner(model, tmp_path, echo)
    exporter = InMemorySpanExporter()
    runner.observability = make_runtime_observability(exporter)
    runner.loop.observability = runner.observability

    result = asyncio.run(
        run_agent(
            runner, agent, "use the Tool", session=Session("observed-tool", InMemorySessionStore()), limits=RunLimits()
        )
    )

    assert result.state.value == "succeeded"
    spans = exporter.get_finished_spans()
    names = [span.name for span in spans]
    assert names.count("pleiad.step") == 2
    assert names.count("pleiad.model.generate") == 2
    assert names.count("pleiad.tool.execute") == 1
    model_spans = [span for span in spans if span.name == "pleiad.model.generate"]
    tool_span = next(span for span in spans if span.name == "pleiad.tool.execute")
    assert model_spans[0].parent is not None
    assert tool_span.parent is not None
    assert model_spans[0].parent.span_id == tool_span.parent.span_id
    assert model_spans[1].parent is not None
    assert model_spans[1].parent.span_id != model_spans[0].parent.span_id


def test_runner_emits_one_generation_for_anthropic_model(tmp_path: Path) -> None:
    model = FakeAnthropicModel((anthropic_text_batch("done", "response-anthropic"),))
    runner, agent = make_runner(model, tmp_path)
    exporter = InMemorySpanExporter()
    runner.observability = make_runtime_observability(exporter)
    runner.loop.observability = runner.observability

    result = asyncio.run(
        run_agent(
            runner, agent, "hello", session=Session("observed-anthropic", InMemorySessionStore()), limits=RunLimits()
        )
    )

    assert result.state.value == "succeeded"
    model_spans = [span for span in exporter.get_finished_spans() if span.name == "pleiad.model.generate"]
    assert len(model_spans) == 1
    assert model_spans[0].attributes is not None
    assert model_spans[0].attributes["pleiad.model.protocol"] == "anthropic"


def test_concurrent_runners_keep_run_and_generation_traces_separate(tmp_path: Path) -> None:
    runner_root_a = tmp_path / "runner-a"
    runner_root_b = tmp_path / "runner-b"
    runner_root_a.mkdir()
    runner_root_b.mkdir()
    runner_a, agent_a = make_runner(FakeOpenAIModel((text_batch("a", "response-a"),)), runner_root_a)
    runner_b, agent_b = make_runner(FakeOpenAIModel((text_batch("b", "response-b"),)), runner_root_b)
    exporter = InMemorySpanExporter()
    observability = make_runtime_observability(exporter)
    runner_a.observability = observability
    runner_a.loop.observability = observability
    runner_b.observability = observability
    runner_b.loop.observability = observability

    async def scenario() -> None:
        results = await asyncio.gather(
            run_agent(
                runner_a, agent_a, "a", session=Session("observed-a", InMemorySessionStore()), limits=RunLimits()
            ),
            run_agent(
                runner_b, agent_b, "b", session=Session("observed-b", InMemorySessionStore()), limits=RunLimits()
            ),
        )
        assert all(result.state.value == "succeeded" for result in results)

    asyncio.run(scenario())

    run_spans = [span for span in exporter.get_finished_spans() if span.name == "pleiad.run"]
    model_spans = [span for span in exporter.get_finished_spans() if span.name == "pleiad.model.generate"]
    assert len(run_spans) == 2
    assert len(model_spans) == 2
    run_trace_ids = {span.context.trace_id for span in run_spans}
    assert len(run_trace_ids) == 2
    assert {span.context.trace_id for span in model_spans} == run_trace_ids
    assert all(span.parent is not None for span in model_spans)


def test_runner_ends_model_span_when_streaming_run_is_cancelled(tmp_path: Path) -> None:
    async def scenario() -> None:
        model = BlockingOpenAIModel()
        runner, agent = make_runner(model, tmp_path)
        exporter = InMemorySpanExporter()
        runner.observability = make_runtime_observability(exporter)
        runner.loop.observability = runner.observability

        task = asyncio.create_task(
            run_agent(
                runner,
                agent,
                "keep streaming",
                session=Session("observed-cancel", InMemorySessionStore()),
                limits=RunLimits(),
            )
        )
        await model.started.wait()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("the cancelled streaming run completed unexpectedly")

        model_spans = [span for span in exporter.get_finished_spans() if span.name == "pleiad.model.generate"]
        assert len(model_spans) == 1
        assert model_spans[0].status.status_code is StatusCode.ERROR

    asyncio.run(scenario())


@pytest.mark.parametrize("reported", [False, True])
def test_runner_telemetry_distinguishes_missing_usage_from_measured_zero(tmp_path: Path, reported: bool) -> None:
    batch = text_batch("done", "usage")
    if reported:
        batch = (
            *batch,
            batch[-1].model_copy(
                update={
                    "usage": OpenAIUsage(
                        prompt_tokens=0,
                        completion_tokens=0,
                        total_tokens=0,
                    )
                }
            ),
        )
    model = FakeOpenAIModel((batch,), report_usage=False)
    runner, agent = make_runner(model, tmp_path)
    exporter = InMemorySpanExporter()
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))
    reader = InMemoryMetricReader()
    meter_provider = MeterProvider(metric_readers=[reader])
    runner.observability = Observability(tracer_provider=tracer_provider, meter_provider=meter_provider)
    runner.loop.observability = runner.observability
    try:
        result = asyncio.run(
            run_agent(
                runner,
                agent,
                "answer",
                session=Session("telemetry-usage", InMemorySessionStore()),
                limits=RunLimits(),
            )
        )
        assert result.state.value == "succeeded"
        span = next(item for item in exporter.get_finished_spans() if item.name == "pleiad.model.generate")
        assert span.attributes is not None
        data = reader.get_metrics_data()
        assert data is not None
        names = {
            metric.name
            for resource in data.resource_metrics
            for scope in resource.scope_metrics
            for metric in scope.metrics
        }
        if reported:
            assert span.attributes["gen_ai.usage.input_tokens"] == 0
            assert span.attributes["gen_ai.usage.output_tokens"] == 0
            assert "pleiad.model.input_tokens" in names
            assert "pleiad.model.output_tokens" in names
        else:
            assert "gen_ai.usage.input_tokens" not in span.attributes
            assert "gen_ai.usage.output_tokens" not in span.attributes
            assert "pleiad.model.input_tokens" not in names
            assert "pleiad.model.output_tokens" not in names
    finally:
        meter_provider.shutdown()
        tracer_provider.shutdown()
