from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import ClassVar


class FakeOpenAIStream:
    """Async stream at the official OpenAI SDK boundary."""

    def __init__(self, chunks: tuple[object, ...]) -> None:
        self.chunks = iter(chunks)
        self.closed = False

    def __aiter__(self) -> AsyncIterator[object]:
        """Return this stream as its async iterator."""
        return self

    async def __anext__(self) -> object:
        """Return the next fake SDK chunk."""
        try:
            return next(self.chunks)
        except StopIteration as error:
            raise StopAsyncIteration from error

    async def close(self) -> None:
        """Record SDK stream cleanup."""
        self.closed = True


class FakeOpenAICompletions:
    """Fake official OpenAI completions resource."""

    def __init__(self, response: object, stream: FakeOpenAIStream) -> None:
        self.response = response
        self.stream = stream
        self.requests: list[dict[str, object]] = []

    async def create(self, **kwargs: object) -> object:
        """Record SDK parameters and return the configured response."""
        self.requests.append(kwargs)
        if kwargs.get("stream") is True:
            return self.stream
        return self.response


class FakeOpenAIChat:
    """Fake official OpenAI chat resource."""

    def __init__(self, completions: FakeOpenAICompletions) -> None:
        self.completions = completions


class FakeOpenAIClient:
    """Fake official OpenAI SDK client."""

    response: ClassVar[object] = SimpleNamespace()
    stream: ClassVar[FakeOpenAIStream | None] = None
    instances: ClassVar[list["FakeOpenAIClient"]] = []

    def __init__(self, **kwargs: object) -> None:
        self.settings = kwargs
        if self.stream is None:
            raise RuntimeError("fake OpenAI stream was not configured")
        self.completions = FakeOpenAICompletions(self.response, self.stream)
        self.chat = FakeOpenAIChat(self.completions)
        type(self).instances.append(self)
        self.closed = False

    async def close(self) -> None:
        """Record SDK client cleanup."""
        self.closed = True


class FakeAnthropicMessages:
    """Fake official Anthropic messages resource."""

    def __init__(self, response: object) -> None:
        self.response = response
        self.requests: list[dict[str, object]] = []

    async def create(self, **kwargs: object) -> object:
        """Record SDK parameters and return the configured response."""
        self.requests.append(kwargs)
        return self.response


class FakeAnthropicClient:
    """Fake official Anthropic SDK client."""

    response: ClassVar[object] = SimpleNamespace()
    instances: ClassVar[list["FakeAnthropicClient"]] = []

    def __init__(self, **kwargs: object) -> None:
        self.settings = kwargs
        self.messages = FakeAnthropicMessages(self.response)
        type(self).instances.append(self)
        self.closed = False

    async def close(self) -> None:
        """Record SDK client cleanup."""
        self.closed = True


class FakeZhipuCompletions:
    """Fake synchronous GLM completions resource."""

    def __init__(self, response: object) -> None:
        self.response = response
        self.requests: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> object:
        """Record SDK parameters and return the configured response."""
        self.requests.append(kwargs)
        return self.response


class FakeZhipuClient:
    """Fake official Zhipu SDK client."""

    response: ClassVar[object] = SimpleNamespace()
    instances: ClassVar[list["FakeZhipuClient"]] = []

    def __init__(self, **kwargs: object) -> None:
        self.settings = kwargs
        self.chat = SimpleNamespace(completions=FakeZhipuCompletions(self.response))
        type(self).instances.append(self)

    def close(self) -> None:
        """Close the fake synchronous SDK client."""


class FakeQwenGeneration:
    """Fake official DashScope asynchronous generation resource."""

    response: ClassVar[object] = SimpleNamespace()
    requests: ClassVar[list[dict[str, object]]] = []

    @classmethod
    async def call(cls, **kwargs: object) -> object:
        """Record DashScope arguments and return the configured envelope."""
        cls.requests.append(kwargs)
        return cls.response


def openai_raw_response() -> object:
    """Return one official-SDK-shaped OpenAI response."""
    return SimpleNamespace(
        id="response-1",
        object="chat.completion",
        model="test-model",
        choices=[
            SimpleNamespace(
                index=0,
                message=SimpleNamespace(
                    role="assistant",
                    content="done",
                    refusal=None,
                    reasoning_content=None,
                    tool_calls=None,
                ),
                finish_reason="stop",
            ),
        ],
        usage=None,
    )


def openai_raw_stream() -> FakeOpenAIStream:
    """Return a text delta followed by a terminal official-SDK chunk."""
    return FakeOpenAIStream(
        (
            SimpleNamespace(
                id="response-1",
                object="chat.completion.chunk",
                model="test-model",
                choices=[
                    SimpleNamespace(
                        index=0,
                        delta=SimpleNamespace(content="done", reasoning_content=None, tool_calls=None),
                        finish_reason=None,
                    ),
                ],
                usage=None,
            ),
            SimpleNamespace(
                id="response-1",
                object="chat.completion.chunk",
                model="test-model",
                choices=[
                    SimpleNamespace(
                        index=0,
                        delta=SimpleNamespace(content=None, reasoning_content=None, tool_calls=None),
                        finish_reason="stop",
                    ),
                ],
                usage=None,
            ),
        ),
    )


def anthropic_raw_response() -> object:
    """Return one official-SDK-shaped Anthropic response."""
    return SimpleNamespace(
        id="message-1",
        type="message",
        role="assistant",
        model="test-model",
        content=[SimpleNamespace(type="text", text="done")],
        stop_reason="end_turn",
        usage=SimpleNamespace(input_tokens=2, output_tokens=3),
    )
