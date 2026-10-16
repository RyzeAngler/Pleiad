from types import SimpleNamespace

import pytest
from anthropic.types import RawContentBlockDeltaEvent, RawMessageDeltaEvent
from openai.types.chat import ChatCompletion, ChatCompletionChunk

from pleiad.contracts import JsonObject
from pleiad.models.anthropic.response import (
    AnthropicContentBlock,
    AnthropicResponse,
    AnthropicUsage,
)
from pleiad.models.anthropic.response import parse_event as parse_anthropic_event
from pleiad.models.anthropic.response import parse_response as parse_anthropic_response
from pleiad.models.openai.response import (
    OpenAIChoice,
    OpenAIResponse,
    OpenAIResponseFunction,
    OpenAIResponseMessage,
    OpenAIResponseToolCall,
    OpenAIStreamChoice,
    OpenAIStreamChunk,
    OpenAIStreamDelta,
    OpenAIUsage,
    parse_stream_chunk,
)
from pleiad.models.openai.response import parse_response as parse_openai_response
from pleiad.models.result import ModelFinishReason, ModelStreamEventKind
from pleiad.runtime.execution.model_input import AnthropicStreamAccumulator, OpenAIStreamAccumulator

from .support import anthropic_raw_response


def test_openai_response_projects_tool_call_to_common_result() -> None:
    """The OpenAI response adapter exposes a normalized tool call internally."""
    response = OpenAIResponse(
        id="response-1",
        model="test-model",
        choices=(
            OpenAIChoice(
                message=OpenAIResponseMessage(
                    content=None,
                    tool_calls=(
                        OpenAIResponseToolCall(
                            id="call-1",
                            function=OpenAIResponseFunction(name="lookup", arguments='{"id": 7}'),
                        ),
                    ),
                ),
                finish_reason="tool_calls",
            ),
        ),
    )

    result = response.to_result(provider="glm")

    assert result.provider == "glm"
    assert result.finish_reason is ModelFinishReason.TOOL_CALL
    assert result.tool_calls[0].arguments == '{"id": 7}'


def test_openai_response_projects_usage_to_common_result() -> None:
    """The OpenAI response adapter exposes normalized token usage internally."""
    response = OpenAIResponse(
        id="response-1",
        model="test-model",
        choices=(
            OpenAIChoice(
                message=OpenAIResponseMessage(content="done"),
                finish_reason="stop",
            ),
        ),
        usage={
            "prompt_tokens": 3,
            "completion_tokens": 4,
            "total_tokens": 7,
            "reasoning_tokens": 1,
        },
    )

    result = response.to_result(provider="openai")

    assert result.usage.input_tokens == 3
    assert result.usage.output_tokens == 4
    assert result.usage.total_tokens == 7
    assert result.usage.reasoning_tokens == 1


def test_anthropic_response_projects_content_and_tool_use_to_common_result() -> None:
    """The Anthropic response adapter normalizes text, thinking, tools, and usage."""
    response = AnthropicResponse(
        id="message-1",
        model="test-model",
        content=(
            AnthropicContentBlock(type="thinking", thinking="reason"),
            AnthropicContentBlock(type="text", text="answer"),
            AnthropicContentBlock(type="tool_use", id="call-1", name="lookup", input={"id": 7}),
        ),
        stop_reason="tool_use",
        usage=AnthropicUsage(input_tokens=3, output_tokens=4),
    )

    result = response.to_result(provider="anthropic")

    assert result.thinking == "reason"
    assert result.text == "answer"
    assert result.finish_reason is ModelFinishReason.TOOL_CALL
    assert result.tool_calls[0].name == "lookup"
    assert result.usage.total_tokens == 7


def test_openai_response_rejects_tool_call_without_identity() -> None:
    """The OpenAI response adapter rejects a tool call missing its required identity."""
    raw = SimpleNamespace(
        id="response-1",
        object="chat.completion",
        model="test-model",
        choices=[
            SimpleNamespace(
                index=0,
                message=SimpleNamespace(
                    role="assistant",
                    content=None,
                    tool_calls=[
                        SimpleNamespace(
                            index=0,
                            id=None,
                            type="function",
                            function=SimpleNamespace(name="lookup", arguments="{}"),
                        ),
                    ],
                ),
                finish_reason="tool_calls",
            ),
        ],
        usage=None,
    )

    with pytest.raises(ValueError, match="tool call id"):
        parse_openai_response(raw)


def test_anthropic_response_parser_accepts_official_sdk_shape() -> None:
    """The Anthropic response adapter reads the official SDK object shape."""
    raw = anthropic_raw_response()

    response = parse_anthropic_response(raw)

    assert response.id == "message-1"
    assert response.content[0].text == "done"
    assert response.to_result(provider="anthropic").usage.total_tokens == 5


def test_anthropic_stream_projects_tool_start_and_message_stop_to_common_events() -> None:
    """The Anthropic stream adapter exposes tool identity and terminal events."""
    tool_start = parse_anthropic_event(
        SimpleNamespace(
            type="content_block_start",
            index=0,
            content_block=SimpleNamespace(type="tool_use", id="call-1", name="lookup", input={}),
        ),
    )
    message_stop = parse_anthropic_event(SimpleNamespace(type="message_stop"))

    tool_events = tool_start.events()
    stop_events = message_stop.events()

    assert tool_events[0].kind is ModelStreamEventKind.TOOL_CALL_DELTA
    assert tool_events[0].tool_call_id == "call-1"
    assert tool_events[0].tool_name == "lookup"
    assert len(stop_events) == 1
    assert stop_events[0].kind is ModelStreamEventKind.COMPLETED


def test_anthropic_stream_accepts_empty_message_start_content() -> None:
    """The Anthropic stream adapter accepts the empty content in message_start."""
    event = parse_anthropic_event(
        SimpleNamespace(
            type="message_start",
            message=SimpleNamespace(
                id="message-1",
                type="message",
                role="assistant",
                model="test-model",
                content=[],
                stop_reason=None,
                usage=SimpleNamespace(input_tokens=2, output_tokens=0),
            ),
        ),
    )

    assert event.message is not None
    assert event.message.content == ()


def test_openai_refusal_only_stream_preserves_fragments_and_completed_message() -> None:
    accumulator = OpenAIStreamAccumulator("test-model", "openai")
    fragments = ("I cannot ", "help with that.")
    for fragment in fragments:
        chunk = parse_stream_chunk(
            ChatCompletionChunk.model_validate(
                {
                    "id": "response-1",
                    "object": "chat.completion.chunk",
                    "created": 0,
                    "model": "test-model",
                    "choices": [{"index": 0, "delta": {"refusal": fragment}, "finish_reason": None}],
                },
            ),
        )
        accumulator.add(chunk)
        assert [(event.kind, event.text) for event in chunk.events()] == [
            (ModelStreamEventKind.REFUSAL_DELTA, fragment),
        ]
    result = accumulator.result()
    assert result.message.content is None
    assert result.message.refusal == "".join(fragments)


@pytest.mark.parametrize(
    ("stop_reason", "expected"),
    [("max_tokens", ModelFinishReason.LENGTH), ("end_turn", ModelFinishReason.STOP)],
)
def test_anthropic_stream_preserves_terminal_usage_and_finish_reason(
    stop_reason: str,
    expected: ModelFinishReason,
) -> None:
    accumulator = AnthropicStreamAccumulator("test-model", "anthropic")
    accumulator.add(
        parse_anthropic_event(
            {
                "type": "message_start",
                "message": {
                    "id": "message-1",
                    "type": "message",
                    "role": "assistant",
                    "model": "test-model",
                    "content": [],
                    "usage": {"input_tokens": 12, "output_tokens": 0, "cache_read_input_tokens": 5},
                },
            }
        )
    )
    text = parse_anthropic_event(
        RawContentBlockDeltaEvent.model_validate(
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "answer"},
            }
        )
    )
    accumulator.add(text)
    assert text.events()[0].text == "answer"
    for output_tokens in (2, 7):
        accumulator.add(
            parse_anthropic_event(
                RawMessageDeltaEvent.model_validate(
                    {
                        "type": "message_delta",
                        "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                        "usage": {"output_tokens": output_tokens},
                    }
                )
            )
        )
    result = accumulator.result()
    assert result.text == "answer"
    assert result.finish_reason is expected
    assert result.usage.input_tokens == 12
    assert result.usage.output_tokens == 7
    assert result.usage.total_tokens == 19
    assert result.usage.cached_input_tokens == 5


def test_openai_response_accepts_official_tool_call_without_stream_index() -> None:
    response = ChatCompletion.model_validate(
        {
            "id": "response-1",
            "object": "chat.completion",
            "created": 0,
            "model": "test-model",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {"id": "call-1", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}
                        ],
                    },
                }
            ],
            "usage": {
                "prompt_tokens": 1,
                "completion_tokens": 2,
                "total_tokens": 3,
                "prompt_tokens_details": {"cached_tokens": 0},
            },
        }
    )
    result = parse_openai_response(response).to_result(provider="openai")
    assert result.tool_calls[0].id == "call-1"
    assert result.tool_calls[0].arguments == "{}"


@pytest.mark.parametrize("missing_field", ["id", "name"])
def test_streamed_tool_call_without_identity_is_rejected(missing_field: str) -> None:
    accumulator = OpenAIStreamAccumulator("test-model", "openai")
    accumulator.add(
        parse_stream_chunk(
            {
                "id": "response-1",
                "object": "chat.completion.chunk",
                "model": "test-model",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": None if missing_field == "id" else "call-1",
                                    "type": "function",
                                    "function": {
                                        "name": None if missing_field == "name" else "lookup",
                                        "arguments": "{}",
                                    },
                                }
                            ]
                        },
                    }
                ],
            }
        )
    )
    with pytest.raises(ValueError, match="missing its id or name"):
        accumulator.result()


@pytest.mark.parametrize("fragments", [(), ('{"id":', "7}")])
def test_anthropic_stream_preserves_empty_tool_input_without_prefixing_argument_fragments(
    fragments: tuple[str, ...],
) -> None:
    accumulator = AnthropicStreamAccumulator("test-model", "anthropic")
    accumulator.add(
        parse_anthropic_event(
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "tool_use", "id": "call-1", "name": "lookup", "input": {}},
            }
        )
    )
    for fragment in fragments:
        accumulator.add(
            parse_anthropic_event(
                RawContentBlockDeltaEvent.model_validate(
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "input_json_delta", "partial_json": fragment},
                    }
                )
            )
        )
    result = accumulator.result()
    assert result.tool_calls[0].id == "call-1"
    assert result.tool_calls[0].name == "lookup"
    assert result.tool_calls[0].arguments == ("".join(fragments) if fragments else "{}")
    assert result.finish_reason is ModelFinishReason.TOOL_CALL


def test_anthropic_stream_cumulative_input_usage_replaces_start_counts() -> None:
    accumulator = AnthropicStreamAccumulator("test-model", "anthropic")
    accumulator.add(
        parse_anthropic_event(
            {
                "type": "message_start",
                "message": {
                    "id": "message-1",
                    "type": "message",
                    "role": "assistant",
                    "model": "test-model",
                    "content": [],
                    "usage": {"input_tokens": 12, "output_tokens": 0, "cache_read_input_tokens": 5},
                },
            }
        )
    )
    accumulator.add(
        parse_anthropic_event(
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "answer"},
            }
        )
    )
    accumulator.add(
        parse_anthropic_event(
            RawMessageDeltaEvent.model_validate(
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn"},
                    "usage": {"input_tokens": 20, "output_tokens": 7, "cache_read_input_tokens": 0},
                }
            )
        )
    )
    usage = accumulator.result().usage
    assert usage.input_tokens == 20
    assert usage.cached_input_tokens == 0
    assert usage.total_tokens == 27


@pytest.mark.parametrize("usage", [None, {}])
def test_openai_missing_usage_remains_unknown(usage: JsonObject | None) -> None:
    response = parse_openai_response(
        {
            "id": "missing-usage",
            "object": "chat.completion",
            "model": "test-model",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "answer"}, "finish_reason": "stop"}],
            "usage": usage,
        }
    )
    measured = response.to_result(provider="openai").usage
    assert measured.input_tokens is None
    assert measured.output_tokens is None
    assert measured.total_tokens is None
    assert measured.reasoning_tokens is None
    assert measured.cached_input_tokens is None


def test_openai_reported_zero_usage_preserves_real_zero() -> None:
    response = parse_openai_response(
        {
            "id": "zero-usage",
            "object": "chat.completion",
            "model": "test-model",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "answer"}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "completion_tokens_details": {"reasoning_tokens": 0},
                "prompt_tokens_details": {"cached_tokens": 0},
            },
        }
    )
    measured = response.to_result(provider="openai").usage
    assert measured.input_tokens == 0
    assert measured.output_tokens == 0
    assert measured.total_tokens == 0
    assert measured.reasoning_tokens == 0
    assert measured.cached_input_tokens == 0


def test_anthropic_partial_stream_usage_does_not_fill_missing_counts_with_zero() -> None:
    accumulator = AnthropicStreamAccumulator("test-model", "anthropic")
    accumulator.add(
        parse_anthropic_event(
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 3},
            }
        )
    )
    accumulator.add(
        parse_anthropic_event(
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "answer"},
            }
        )
    )
    measured = accumulator.result().usage
    assert measured.input_tokens is None
    assert measured.output_tokens == 3
    assert measured.total_tokens is None
    assert measured.cached_input_tokens is None


def test_anthropic_message_start_usage_does_not_claim_final_output_usage() -> None:
    accumulator = AnthropicStreamAccumulator("test-model", "anthropic")
    accumulator.add(
        parse_anthropic_event(
            {
                "type": "message_start",
                "message": {
                    "id": "start-only",
                    "type": "message",
                    "role": "assistant",
                    "model": "test-model",
                    "content": [],
                    "usage": {"input_tokens": 7, "output_tokens": 0},
                },
            }
        )
    )
    accumulator.add(
        parse_anthropic_event(
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "answer"},
            }
        )
    )
    measured = accumulator.result().usage
    assert measured.input_tokens == 7
    assert measured.output_tokens is None
    assert measured.total_tokens is None


def test_openai_stream_retains_known_counts_when_later_usage_fields_are_omitted() -> None:
    accumulator = OpenAIStreamAccumulator("test-model", "openai")
    accumulator.add(
        OpenAIStreamChunk(
            model="test-model",
            choices=(OpenAIStreamChoice(delta=OpenAIStreamDelta(content="answer")),),
            usage=OpenAIUsage(prompt_tokens=2),
        )
    )
    accumulator.add(
        OpenAIStreamChunk(
            model="test-model",
            usage=OpenAIUsage(completion_tokens=0, prompt_tokens_details={"cached_tokens": 0}),
        )
    )
    measured = accumulator.result().usage
    assert measured.input_tokens == 2
    assert measured.output_tokens == 0
    assert measured.total_tokens == 2
    assert measured.cached_input_tokens == 0
    assert measured.reasoning_tokens is None
