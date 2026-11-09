import asyncio
import json
from collections.abc import AsyncIterator

from pleiad.contracts import JsonObject
from pleiad.errors.base import SystemFault
from pleiad.models.openai.request import OpenAIRequest
from pleiad.models.openai.response import (
    OpenAIResponse,
    OpenAIStreamChoice,
    OpenAIStreamChunk,
    OpenAIStreamDelta,
    OpenAIStreamFunction,
    OpenAIStreamToolCall,
    OpenAIUsage,
)

PINNED_IMAGE = "python:3.12-slim@sha256:" + "a" * 64


class ScriptedProvider:
    def __init__(self, responses: tuple[str | JsonObject, ...], *, report_usage: bool = True) -> None:
        self.responses = iter(responses)
        self.report_usage = report_usage
        self.requests: list[OpenAIRequest] = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.block = False
        self.fail = False
        self.closed = False
        self.cancelled = False

    async def complete(self, request: OpenAIRequest) -> OpenAIResponse:
        raise AssertionError("Weave should consume the shared SDK streaming execution path")

    async def stream(self, request: OpenAIRequest) -> AsyncIterator[OpenAIStreamChunk]:
        self.requests.append(request)
        self.started.set()
        try:
            if self.block:
                await self.release.wait()
            if self.fail:
                raise SystemFault("model.provider_unavailable", "Provider unavailable")
            response = next(self.responses)
            if isinstance(response, dict):
                call = OpenAIStreamToolCall(
                    index=0,
                    id=f"call-{len(self.requests)}",
                    function=OpenAIStreamFunction(
                        name=str(response["name"]), arguments=json.dumps(response["arguments"])
                    ),
                )
                delta = OpenAIStreamDelta(tool_calls=(call,))
                finish = "tool_calls"
            else:
                delta = OpenAIStreamDelta(content=response)
                finish = "stop"
            yield OpenAIStreamChunk(model="test-model", choices=(OpenAIStreamChoice(delta=delta),))
            yield OpenAIStreamChunk(
                model="test-model",
                choices=(OpenAIStreamChoice(delta=OpenAIStreamDelta(), finish_reason=finish),),
                usage=OpenAIUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2) if self.report_usage else None,
            )
        except asyncio.CancelledError:
            self.cancelled = True
            raise

    async def aclose(self) -> None:
        self.closed = True
