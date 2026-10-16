import asyncio
import logging
from dataclasses import dataclass

from langfuse import Langfuse
from langfuse.span_filter import is_default_export_span
from langfuse.types import MaskOtelSpansParams, MaskOtelSpansResult, OtelSpanIdentifier, OtelSpanPatch
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SpanExporter

from pleiad.observability.attributes import PRIVACY_MASKED, SCOPE_NAME
from pleiad.observability.config import LangfuseConfig
from pleiad.observability.errors import ObservabilityInitializationError
from pleiad.observability.tracing import Observability

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ShutdownResult:
    """Describe a bounded Langfuse shutdown attempt."""

    completed: bool
    timed_out: bool
    in_progress: bool = False


class LangfuseObservability:
    """Own one Langfuse client and its private OpenTelemetry provider."""

    def __init__(
        self,
        config: LangfuseConfig,
        client: Langfuse,
        provider: TracerProvider,
        observability: Observability,
    ) -> None:
        """Bind resources created by ``create_langfuse_observability``."""
        self.config = config
        self.client = client
        self.provider = provider
        self.observability = observability
        self.closed = False
        self.flush_task: asyncio.Task[None] | None = None
        self.shutdown_task: asyncio.Task[None] | None = None

    async def flush(self) -> bool:
        """Flush owned Langfuse data within the configured budget."""
        if self.closed:
            return True
        if self.shutdown_task is not None:
            return False
        if self.flush_task is None:
            self.flush_task = asyncio.create_task(asyncio.to_thread(self.client.flush))
        flush_task = self.flush_task
        try:
            await asyncio.wait_for(asyncio.shield(flush_task), self.config.shutdown_timeout_seconds)
        except TimeoutError:
            logger.error("langfuse flush exceeded its bounded wait")
            return False
        except Exception:
            logger.exception("langfuse flush failed")
            return False
        finally:
            if flush_task.done() and self.flush_task is flush_task:
                self.flush_task = None
        return True

    async def shutdown(self) -> ShutdownResult:
        """Shutdown owned Langfuse resources with a bounded asynchronous wait."""
        if self.closed:
            return ShutdownResult(completed=True, timed_out=False)
        if self.flush_task is not None:
            flush_task = self.flush_task
            try:
                await asyncio.wait_for(asyncio.shield(flush_task), self.config.shutdown_timeout_seconds)
            except TimeoutError:
                logger.error("langfuse shutdown is waiting for an in-progress flush")
                return ShutdownResult(completed=False, timed_out=True, in_progress=True)
            except Exception:
                logger.exception("langfuse flush failed before shutdown")
            finally:
                if flush_task.done() and self.flush_task is flush_task:
                    self.flush_task = None
        if self.shutdown_task is None:
            self.shutdown_task = asyncio.create_task(asyncio.to_thread(self.client.shutdown))
        try:
            await asyncio.wait_for(asyncio.shield(self.shutdown_task), self.config.shutdown_timeout_seconds)
        except TimeoutError:
            logger.error("langfuse shutdown exceeded its bounded wait")
            return ShutdownResult(completed=False, timed_out=True, in_progress=True)
        except Exception:
            logger.exception("langfuse shutdown failed")
            return ShutdownResult(completed=False, timed_out=False)
        self.closed = True
        return ShutdownResult(completed=True, timed_out=False)


def create_langfuse_observability(
    config: LangfuseConfig,
    *,
    span_exporter: SpanExporter | None = None,
) -> LangfuseObservability:
    """Create an explicitly configured Langfuse-owned OTel provider."""
    resource_attributes: dict[str, str] = {"service.name": config.service_name}
    if config.environment is not None:
        resource_attributes["deployment.environment"] = config.environment
    if config.release is not None:
        resource_attributes["service.version"] = config.release
    provider = TracerProvider(resource=Resource.create(resource_attributes))
    try:
        client = Langfuse(
            public_key=config.public_key.get_secret_value(),
            secret_key=config.secret_key.get_secret_value(),
            base_url=config.base_url,
            timeout=config.request_timeout_seconds,
            environment=config.environment,
            release=config.release,
            sample_rate=config.sample_rate,
            flush_at=config.flush_at,
            flush_interval=config.flush_interval_seconds,
            should_export_span=should_export_span,
            mask_otel_spans=mask_otel_spans,
            tracer_provider=provider,
            span_exporter=span_exporter,
        )
    except Exception as error:
        try:
            provider.shutdown()
        except Exception:
            logger.exception("owned OpenTelemetry provider cleanup failed after Langfuse initialization")
        raise ObservabilityInitializationError("Langfuse client initialization failed.") from error
    observability = Observability(config=config, tracer_provider=provider)
    return LangfuseObservability(config, client, provider, observability)


def should_export_span(span: ReadableSpan) -> bool:
    """Export Langfuse defaults plus the complete Pleiad instrumentation scope."""
    scope = span.instrumentation_scope
    return is_default_export_span(span) or (scope is not None and scope.name == SCOPE_NAME)


def mask_otel_spans(*, params: MaskOtelSpansParams) -> MaskOtelSpansResult | None:
    """Remove accidental content-like attributes at the Langfuse export boundary."""
    patches: dict[OtelSpanIdentifier, OtelSpanPatch] = {}
    for identifier, span in params.spans.items():
        deleted = tuple(
            name
            for name in span.attributes
            if name
            in {
                "gen_ai.prompt",
                "gen_ai.completion",
                "gen_ai.input.messages",
                "gen_ai.output.messages",
                "llm.input_messages",
                "llm.output_messages",
            }
        )
        if deleted:
            patches[identifier] = OtelSpanPatch(delete_attributes=deleted, set_attributes={PRIVACY_MASKED: True})
    return None if not patches else MaskOtelSpansResult(span_patches=patches)
