import logging

from opentelemetry.metrics import Counter, Histogram, Meter, NoOpMeterProvider

from pleiad.observability.attributes import SCOPE_NAME

logger = logging.getLogger(__name__)


class ObservabilityMetrics:
    """Record low-cardinality runtime metrics through the host MeterProvider."""

    def __init__(self, meter: Meter | None = None) -> None:
        """Create instruments without configuring or exporting a MeterProvider."""
        selected = NoOpMeterProvider().get_meter(SCOPE_NAME) if meter is None else meter
        try:
            self.bind(selected)
        except Exception:
            logger.exception("observability metric initialization failed")
            self.bind(NoOpMeterProvider().get_meter(SCOPE_NAME))

    def bind(self, meter: Meter) -> None:
        """Bind the fixed SDK instruments to one selected Meter."""
        self.run_count: Counter = meter.create_counter("pleiad.run.count", unit="{run}")
        self.model_count: Counter = meter.create_counter("pleiad.model.call.count", unit="{call}")
        self.tool_failure_count: Counter = meter.create_counter("pleiad.tool.failure.count", unit="{failure}")
        self.model_duration: Histogram = meter.create_histogram("pleiad.model.duration", unit="s")
        self.tool_duration: Histogram = meter.create_histogram("pleiad.tool.duration", unit="s")
        self.model_input_tokens: Histogram = meter.create_histogram("pleiad.model.input_tokens", unit="{token}")
        self.model_output_tokens: Histogram = meter.create_histogram("pleiad.model.output_tokens", unit="{token}")

    def record_run(self, status: str) -> None:
        """Count one Run by controlled terminal status."""
        try:
            self.run_count.add(1, {"status": status})
        except Exception:
            logger.exception("observability run metric failed")

    def record_model(
        self,
        provider: str,
        status: str,
        duration_seconds: float,
        *,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
    ) -> None:
        """Record one model call and its provider-reported usage."""
        attributes = {"provider": provider, "status": status}
        try:
            self.model_count.add(1, attributes)
            self.model_duration.record(duration_seconds, attributes)
            if input_tokens is not None:
                self.model_input_tokens.record(input_tokens, {"provider": provider})
            if output_tokens is not None:
                self.model_output_tokens.record(output_tokens, {"provider": provider})
        except Exception:
            logger.exception("observability model metric failed")

    def record_tool(self, status: str, duration_seconds: float, *, failed: bool) -> None:
        """Record one Tool execution without user-controlled labels."""
        attributes = {"status": status}
        try:
            self.tool_duration.record(duration_seconds, attributes)
            if failed:
                self.tool_failure_count.add(1, attributes)
        except Exception:
            logger.exception("observability tool metric failed")
