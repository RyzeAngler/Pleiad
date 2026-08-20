"""Provider-neutral client protocol for the SDK-backed LLM layer."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from pleiad.llm.capabilities import LLMCapabilities
from pleiad.llm.config import LLMProtocol, LLMProvider
from pleiad.llm.contracts import LLMRequest
from pleiad.llm.response.envelope import LLMResponse


class LLMStreamEventKind(StrEnum):
    """Normalized event categories emitted by an SDK streaming response."""

    THINKING_DELTA = "thinking_delta"
    TEXT_DELTA = "text_delta"
    TOOL_CALL_DELTA = "tool_call_delta"
    COMPLETED = "completed"


@dataclass(frozen=True, slots=True)
class LLMStreamEvent:
    """One provider-neutral piece of a live LLM response."""

    kind: LLMStreamEventKind
    text: str = ""
    response: LLMResponse | None = None
    tool_call_index: int | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    arguments_delta: str = ""


class LLMClient(Protocol):
    """Provider-neutral client returned by the LLM factory."""

    @property
    def capabilities(self) -> LLMCapabilities:
        """Return effective provider capabilities."""

    @property
    def protocol(self) -> LLMProtocol:
        """Return the selected public protocol."""

    @property
    def provider(self) -> LLMProvider:
        """Return the selected provider."""

    @property
    def model(self) -> str:
        """Return the concrete model configured for this client."""

    async def complete(self, request: LLMRequest) -> LLMResponse:
        """Complete one provider-neutral request through an official SDK."""

    def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        """Stream one provider-neutral response through an official SDK."""

    async def aclose(self) -> None:
        """Release SDK client resources."""
