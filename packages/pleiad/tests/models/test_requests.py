import pytest

from pleiad.contracts import JsonValue
from pleiad.messages.message import AssistantMessage
from pleiad.models.anthropic.request import AnthropicRequest
from pleiad.models.openai.request import OpenAIRequest
from pleiad.runtime.execution.model_input import ModelInput, anthropic_messages, openai_messages


@pytest.mark.parametrize(
    ("protocol_request", "field", "value"),
    [
        (
            OpenAIRequest.model_validate({"messages": [{"role": "user", "content": "hello"}]}),
            "temperature",
            0.2,
        ),
        (
            AnthropicRequest.model_validate(
                {"messages": [{"role": "user", "content": "hello"}], "max_tokens": 16},
            ),
            "top_k",
            2,
        ),
    ],
    ids=["openai", "anthropic"],
)
def test_protocol_request_rejects_provider_option_overriding_protocol_field(
    protocol_request: OpenAIRequest | AnthropicRequest,
    field: str,
    value: JsonValue,
) -> None:
    """Protocol fields remain owned by the caller request, not provider options."""
    with pytest.raises(ValueError, match="cannot override protocol field"):
        protocol_request.payload(model="test-model", provider_options={field: value})


@pytest.mark.parametrize("content", [None, "A safe alternative."])
def test_refusal_history_is_preserved_in_both_protocol_requests(content: str | None) -> None:
    model_input = ModelInput(
        instructions="Help safely.",
        messages=(AssistantMessage(content=content, refusal="I cannot do that."),),
        tools=(),
        max_output_tokens=16,
        output_schema=None,
    )
    openai_payload = OpenAIRequest(messages=openai_messages(model_input)).payload(
        model="test-model",
        provider_options={},
    )
    assert openai_payload["messages"] == [
        {"role": "system", "content": "Help safely."},
        {"role": "assistant", "refusal": "I cannot do that.", **({"content": content} if content else {})},
    ]
    anthropic_payload = AnthropicRequest(messages=anthropic_messages(model_input), max_tokens=16).payload(
        model="test-model",
        provider_options={},
    )
    expected_blocks = [] if content is None else [{"type": "text", "text": content}]
    expected_blocks.append({"type": "text", "text": "I cannot do that."})
    assert anthropic_payload["messages"] == [{"role": "assistant", "content": expected_blocks}]
