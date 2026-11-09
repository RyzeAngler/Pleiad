import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from pleiad.observability.attributes import AttributeValue, attribute_values

logger = logging.getLogger(__name__)

TextMasker = Callable[[str], str]

ALLOWED_ATTRIBUTE_NAMES = frozenset(
    {
        "deployment.environment",
        "gen_ai.operation.name",
        "gen_ai.input.content",
        "gen_ai.input.content.bytes",
        "gen_ai.input.content.truncated",
        "gen_ai.output.content",
        "gen_ai.output.content.bytes",
        "gen_ai.output.content.truncated",
        "gen_ai.provider.name",
        "gen_ai.request.model",
        "gen_ai.response.finish_reasons",
        "gen_ai.usage.input_tokens",
        "gen_ai.usage.output_tokens",
        "langfuse.observation.type",
        "pleiad.agent.name",
        "pleiad.call.id",
        "pleiad.context.candidate_count",
        "pleiad.context.tool_count",
        "pleiad.context.step_count",
        "pleiad.error.code",
        "pleiad.error.type",
        "pleiad.graph.id",
        "pleiad.model.first_output_ms",
        "pleiad.model.protocol",
        "pleiad.model.usage.cached_input_tokens",
        "pleiad.model.usage.reasoning_tokens",
        "pleiad.model.usage.total_tokens",
        "pleiad.node.id",
        "pleiad.parent_run.id",
        "pleiad.privacy.masked",
        "pleiad.run.id",
        "pleiad.run.name",
        "pleiad.sandbox.backend",
        "pleiad.sandbox.exit_code",
        "pleiad.sandbox.timed_out",
        "pleiad.status",
        "pleiad.tool.code",
        "pleiad.tool.name",
        "pleiad.step.id",
        "service.name",
        "service.version",
    }
)


@dataclass(frozen=True, slots=True)
class CapturedText:
    """Bounded text selected for explicit content collection."""

    value: str
    original_bytes: int
    truncated: bool


class PrivacyPolicy:
    """Apply default-deny content collection and bounded attributes."""

    def __init__(
        self,
        *,
        capture_inputs: bool = False,
        capture_outputs: bool = False,
        max_content_bytes: int = 4096,
        max_attribute_bytes: int = 512,
        max_attributes: int = 32,
        max_array_items: int = 16,
        max_total_attribute_bytes: int = 8192,
        masker: TextMasker | None = None,
    ) -> None:
        """Create a privacy policy for one observation owner."""
        if (
            min(
                max_content_bytes,
                max_attribute_bytes,
                max_attributes,
                max_array_items,
                max_total_attribute_bytes,
            )
            < 1
        ):
            raise ValueError("privacy byte limits must be positive")
        self.capture_inputs = capture_inputs
        self.capture_outputs = capture_outputs
        self.max_content_bytes = max_content_bytes
        self.max_attribute_bytes = max_attribute_bytes
        self.max_attributes = max_attributes
        self.max_array_items = max_array_items
        self.max_total_attribute_bytes = max_total_attribute_bytes
        self.masker = masker

    def sanitize_attributes(self, values: Mapping[str, object]) -> dict[str, AttributeValue]:
        """Remove sensitive keys and bound the remaining OTel attributes."""
        safe: dict[str, object] = {}
        total_bytes = 0
        for name, value in values.items():
            if len(safe) >= self.max_attributes or name not in ALLOWED_ATTRIBUTE_NAMES:
                continue
            if isinstance(value, str):
                bounded, _, _ = self.bound_text(value, self.max_attribute_bytes)
                candidate: object = bounded
            elif isinstance(value, list | tuple):
                candidate = tuple(self.bound_item(item) for item in value[: self.max_array_items])
            else:
                candidate = value
            candidate_bytes = self.attribute_size(name, candidate)
            if total_bytes + candidate_bytes > self.max_total_attribute_bytes:
                continue
            safe[name] = candidate
            total_bytes += candidate_bytes
        return attribute_values(safe)

    def capture_text(self, text: str, *, field: str) -> CapturedText | None:
        """Mask and bound one explicitly requested content field."""
        enabled = self.capture_inputs if field == "input" else self.capture_outputs if field == "output" else False
        if not enabled:
            return None
        masked = text
        if self.masker is not None:
            try:
                masked = self.masker(text)
            except Exception:
                logger.error("observability content masking failed; content was discarded")
                return None
        value, original_bytes, truncated = self.bound_text(masked, self.max_content_bytes)
        return CapturedText(value=value, original_bytes=original_bytes, truncated=truncated)

    def sensitive_name(self, name: str) -> bool:
        """Return whether an attribute name is denied by the default policy."""
        lowered = name.lower()
        exact = {
            "gen_ai.prompt",
            "gen_ai.completion",
            "gen_ai.input.messages",
            "gen_ai.output.messages",
            "llm.input_messages",
            "llm.output_messages",
            "http.url",
            "url.full",
            "exception.message",
            "exception.stacktrace",
        }
        denied_parts = ("prompt", "completion", "arguments", "stdout", "stderr", "command", "traceback")
        return lowered in exact or any(part in lowered for part in denied_parts)

    def truncate(self, text: str, limit: int) -> tuple[str, bool]:
        """Return text limited by UTF-8 bytes and whether truncation occurred."""
        value, _, truncated = self.bound_text(text, limit)
        return value, truncated

    def bound_text(self, text: str, limit: int) -> tuple[str, int, bool]:
        """Bound UTF-8 text without materializing a second full-size byte string."""
        kept: list[str] = []
        kept_bytes = 0
        original_bytes = 0
        accepting = True
        for character in text:
            encoded_size = len(character.encode("utf-8"))
            original_bytes += encoded_size
            if accepting and kept_bytes + encoded_size <= limit:
                kept.append(character)
                kept_bytes += encoded_size
            else:
                accepting = False
        return "".join(kept), original_bytes, original_bytes > kept_bytes

    def bound_item(self, value: object) -> object:
        """Bound one supported array item before OpenTelemetry conversion."""
        if isinstance(value, str):
            return self.bound_text(value, self.max_attribute_bytes)[0]
        return value

    def attribute_size(self, name: str, value: object) -> int:
        """Estimate the bounded UTF-8 payload size of one attribute."""
        size = len(name.encode("utf-8"))
        if isinstance(value, str):
            return size + len(value.encode("utf-8"))
        if isinstance(value, list | tuple):
            return size + sum(len(str(item).encode("utf-8")) for item in value)
        return size + len(str(value).encode("utf-8"))
