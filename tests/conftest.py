"""Shared fake model fixtures for runtime tests."""

from collections.abc import AsyncIterator

from pleiad.llm.capabilities import LLMCapabilities
from pleiad.llm.client import LLMStreamEvent, LLMStreamEventKind
from pleiad.llm.config import LLMProtocol, LLMProvider
from pleiad.llm.contracts import LLMRequest, ResponseFormatType
from pleiad.llm.response.envelope import LLMResponse
from pleiad.llm.response.parts import ResponsePart, TextPart, ThinkingPart, ToolCallPart


class FakeLLMClient:
    """Deterministic response queue implementing the Model client boundary."""

    protocol = LLMProtocol.OPENAI
    provider = LLMProvider.OPENAI
    model = "fake-model"
    capabilities = LLMCapabilities(
        response_formats=frozenset({ResponseFormatType.TEXT, ResponseFormatType.JSON_OBJECT}),
        tool_calling=True,
    )

    def __init__(self, responses: list[tuple[ResponsePart, ...]]) -> None:
        self.responses = list(responses)
        self.requests: list[LLMRequest] = []

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        parts = self.responses.pop(0)
        return LLMResponse(
            request_id=request.request_id,
            protocol=self.protocol,
            provider=self.provider,
            model=self.model,
            parts=parts,
        )

    async def aclose(self) -> None:
        return None

    async def _stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        self.requests.append(request)
        parts = self.responses.pop(0)
        for part in parts:
            if isinstance(part, TextPart):
                yield LLMStreamEvent(LLMStreamEventKind.TEXT_DELTA, text=part.text)
            elif isinstance(part, ThinkingPart) and part.text:
                yield LLMStreamEvent(LLMStreamEventKind.THINKING_DELTA, text=part.text)
            elif isinstance(part, ToolCallPart):
                yield LLMStreamEvent(
                    LLMStreamEventKind.TOOL_CALL_DELTA,
                    tool_call_index=0,
                    tool_call_id=part.call_id,
                    tool_name=part.name,
                    arguments_delta=part.raw_arguments or "",
                )
        yield LLMStreamEvent(
            LLMStreamEventKind.COMPLETED,
            response=LLMResponse(
                request_id=request.request_id,
                protocol=self.protocol,
                provider=self.provider,
                model=self.model,
                parts=parts,
            ),
        )

    def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        return self._stream(request)
