import asyncio
import threading

import pytest
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import SecretStr

from pleiad.observability.config import LangfuseConfig
from pleiad.observability.errors import ObservabilityInitializationError
from pleiad.observability.integrations import langfuse as langfuse_module
from pleiad.observability.integrations.langfuse import create_langfuse_observability


def test_langfuse_initialization_uses_owned_provider_without_network() -> None:
    async def scenario() -> None:
        exporter = InMemorySpanExporter()
        integration = create_langfuse_observability(
            LangfuseConfig(
                public_key=SecretStr("pk-lf-test"),
                secret_key=SecretStr("".join(("sk", "-lf-test"))),
                base_url="http://127.0.0.1:9",
                request_timeout_seconds=1,
                shutdown_timeout_seconds=1.0,
            ),
            span_exporter=exporter,
        )

        with integration.observability.span("pleiad.run", observation_type="agent") as span:
            integration.observability.finish(span, "succeeded")
        shutdown = await integration.shutdown()

        assert shutdown.completed is True
        exported_span = exporter.get_finished_spans()[0]
        assert exported_span.name == "pleiad.run"
        assert exported_span.resource.attributes["service.name"] == "pleiad"
        scope = exported_span.instrumentation_scope
        assert scope is not None
        assert scope.name == "pleiad"

    asyncio.run(scenario())


def test_langfuse_initialization_failure_shuts_down_owned_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    providers: list[TracerProvider] = []

    class RecordingTracerProvider(TracerProvider):
        was_shutdown = False

        def shutdown(self) -> None:
            self.was_shutdown = True
            super().shutdown()

    def create_provider(*, resource: Resource) -> TracerProvider:
        provider = RecordingTracerProvider(resource=resource)
        providers.append(provider)
        return provider

    def fail_client_initialization(**kwargs: object) -> None:
        raise RuntimeError("client initialization failed")

    monkeypatch.setattr(langfuse_module, "TracerProvider", create_provider)
    monkeypatch.setattr(langfuse_module, "Langfuse", fail_client_initialization)

    with pytest.raises(ObservabilityInitializationError) as caught:
        create_langfuse_observability(
            LangfuseConfig(
                public_key=SecretStr("pk-lf-test"),
                secret_key=SecretStr("sk-lf-test"),
                base_url="http://127.0.0.1:9",
                request_timeout_seconds=1,
                shutdown_timeout_seconds=1.0,
            )
        )

    assert isinstance(caught.value.__cause__, RuntimeError)
    assert len(providers) == 1
    assert isinstance(providers[0], RecordingTracerProvider)
    assert providers[0].was_shutdown is True


def test_langfuse_shutdown_timeout_retains_one_owned_background_task(monkeypatch: pytest.MonkeyPatch) -> None:
    """A timed-out shutdown remains owned and a retry awaits the same call."""

    async def scenario() -> None:
        integration = create_langfuse_observability(
            LangfuseConfig(
                public_key=SecretStr("pk-lf-test"),
                secret_key=SecretStr("".join(("sk", "-lf-test"))),
                base_url="http://127.0.0.1:9",
                request_timeout_seconds=1,
                shutdown_timeout_seconds=0.01,
            ),
            span_exporter=InMemorySpanExporter(),
        )
        release = threading.Event()
        calls = 0
        original_shutdown = integration.client.shutdown

        def blocking_shutdown() -> None:
            nonlocal calls
            calls += 1
            release.wait(1)
            original_shutdown()

        monkeypatch.setattr(integration.client, "shutdown", blocking_shutdown)

        first = await integration.shutdown()
        release.set()
        second = await integration.shutdown()

        assert first.timed_out is True
        assert first.in_progress is True
        assert second.completed is True
        assert calls == 1

    asyncio.run(scenario())


def test_concurrent_langfuse_flush_callers_share_one_successful_flush(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        integration = create_langfuse_observability(
            LangfuseConfig(
                public_key=SecretStr("pk-lf-concurrent"),
                secret_key=SecretStr("sk-lf-concurrent"),
                base_url="http://127.0.0.1:9",
                shutdown_timeout_seconds=1,
            ),
            span_exporter=InMemorySpanExporter(),
        )
        started = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()
        calls = 0

        def blocking_flush() -> None:
            nonlocal calls
            calls += 1
            loop.call_soon_threadsafe(started.set)
            release.wait(2)

        monkeypatch.setattr(integration.client, "flush", blocking_flush)
        try:
            async with asyncio.TaskGroup() as group:
                first = group.create_task(integration.flush())
                await started.wait()
                second = group.create_task(integration.flush())
                loop.call_soon(release.set)
            assert first.result() is True
            assert second.result() is True
            assert calls == 1
        finally:
            release.set()
            await integration.shutdown()

    asyncio.run(scenario())
