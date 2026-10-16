import asyncio
import logging
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from time import monotonic

from opentelemetry.metrics import Meter, MeterProvider, NoOpMeterProvider
from opentelemetry.trace import INVALID_SPAN, NoOpTracerProvider, Span, Status, StatusCode, Tracer, TracerProvider

from pleiad.errors.base import PleiadError
from pleiad.observability.attributes import (
    ERROR_CODE,
    ERROR_TYPE,
    SCOPE_NAME,
    STATUS,
    AttributeValue,
    attribute_values,
)
from pleiad.observability.config import ObservabilityConfig
from pleiad.observability.metrics import ObservabilityMetrics
from pleiad.observability.privacy import PrivacyPolicy

logger = logging.getLogger(__name__)


class Observability:
    """Provide small, provider-neutral helpers around the OpenTelemetry API."""

    def __init__(
        self,
        *,
        config: ObservabilityConfig | None = None,
        tracer_provider: TracerProvider | None = None,
        meter_provider: MeterProvider | None = None,
        privacy: PrivacyPolicy | None = None,
    ) -> None:
        """Use only explicitly supplied providers, otherwise keep telemetry disabled."""
        self.config = ObservabilityConfig() if config is None else config
        self.privacy = (
            PrivacyPolicy(
                capture_inputs=self.config.capture_inputs,
                capture_outputs=self.config.capture_outputs,
                max_content_bytes=self.config.max_content_bytes,
                max_attribute_bytes=self.config.max_attribute_bytes,
                max_attributes=self.config.max_attributes,
                max_array_items=self.config.max_array_items,
                max_total_attribute_bytes=self.config.max_total_attribute_bytes,
            )
            if privacy is None
            else privacy
        )
        selected_tracer_provider = NoOpTracerProvider() if tracer_provider is None else tracer_provider
        selected_meter_provider = NoOpMeterProvider() if meter_provider is None else meter_provider
        self.tracer: Tracer = selected_tracer_provider.get_tracer(SCOPE_NAME)
        self.meter: Meter = selected_meter_provider.get_meter(SCOPE_NAME)
        self.metrics = ObservabilityMetrics(self.meter)

    @contextmanager
    def span(
        self,
        name: str,
        *,
        attributes: Mapping[str, object] | None = None,
        observation_type: str = "span",
    ) -> Iterator[Span]:
        """Create one current OTel span while keeping exception content private."""
        values = {} if attributes is None else dict(attributes)
        values["langfuse.observation.type"] = observation_type
        try:
            safe_attributes = self.privacy.sanitize_attributes(values)
            manager = self.tracer.start_as_current_span(
                name,
                attributes=safe_attributes,
                record_exception=False,
                set_status_on_exception=False,
            )
            span = manager.__enter__()
        except Exception:
            logger.exception("observability span creation failed")
            yield INVALID_SPAN
            return
        business_error: BaseException | None = None
        try:
            yield span
        except asyncio.CancelledError as error:
            business_error = error
            self.finish(span, "cancelled")
            raise
        except TimeoutError as error:
            business_error = error
            self.finish(span, "timeout")
            raise
        except Exception as error:
            business_error = error
            self.finish(span, "failed", error=error)
            raise
        finally:
            try:
                if business_error is None:
                    manager.__exit__(None, None, None)
                else:
                    manager.__exit__(type(business_error), business_error, business_error.__traceback__)
            except Exception:
                logger.exception("observability span close failed")

    def finish(
        self,
        span: Span,
        status: str,
        *,
        error: BaseException | None = None,
        error_code: str | None = None,
        error_type: str | None = None,
    ) -> None:
        """Set bounded terminal state on an OTel span without recording an exception."""
        values: dict[str, object] = {STATUS: status}
        if isinstance(error, PleiadError):
            values[ERROR_CODE] = error.code
            values[ERROR_TYPE] = error.type.value
        elif error_code is not None:
            values[ERROR_CODE] = error_code
            if error_type is not None:
                values[ERROR_TYPE] = error_type
        self.set_attributes(span, values)
        try:
            span.set_status(StatusCode.OK if status in {"succeeded", "success", "ok"} else Status(StatusCode.ERROR))
        except Exception:
            logger.exception("observability span status update failed")

    def set_attribute(self, span: Span, name: str, value: object) -> None:
        """Set one sanitized attribute while isolating instrumentation failures."""
        self.set_attributes(span, {name: value})

    def set_attributes(self, span: Span, values: Mapping[str, object]) -> None:
        """Set sanitized attributes while isolating instrumentation failures."""
        try:
            safe = self.privacy.sanitize_attributes(values)
            for name, value in safe.items():
                span.set_attribute(name, value)
        except Exception:
            logger.exception("observability span attribute update failed")

    def capture_text(self, span: Span, text: str, *, field: str) -> None:
        """Attach explicitly enabled, masked, bounded content and its size facts."""
        captured = self.privacy.capture_text(text, field=field)
        if captured is None:
            return
        attribute_name = "gen_ai.input.content" if field == "input" else "gen_ai.output.content"
        self.set_attributes(
            span,
            {
                attribute_name: captured.value,
                f"{attribute_name}.bytes": captured.original_bytes,
                f"{attribute_name}.truncated": captured.truncated,
            },
        )

    def start_time(self) -> float:
        """Return a monotonic timestamp for duration measurements."""
        return monotonic()

    def duration(self, started: float) -> float:
        """Return elapsed monotonic seconds."""
        return max(0.0, monotonic() - started)

    def safe_attributes(self, values: Mapping[str, object]) -> dict[str, AttributeValue]:
        """Return sanitized OTel attributes for callers that need a snapshot."""
        return attribute_values(self.privacy.sanitize_attributes(values))
