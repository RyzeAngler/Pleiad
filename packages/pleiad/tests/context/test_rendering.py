from pleiad.context.rendering import ContextRenderer
from pleiad.context.selection.models import ContextBlock, ContextView
from pleiad.messages.message import AssistantMessage, TextBlock, ToolCall, ToolMessage, UserMessage


def test_context_renderer_keeps_dynamic_references_request_local() -> None:
    """Rendering labels references while leaving the ShortMemory User message unchanged."""
    original = UserMessage(content=(TextBlock(text="Answer this."), TextBlock(text="attached")))
    view = ContextView((ContextBlock(kind="knowledge", origin="doc-1", content="untrusted note"),))

    rendered = ContextRenderer().render(original, context_view=view)

    assert original.content == (TextBlock(text="Answer this."), TextBlock(text="attached"))
    assert not isinstance(rendered.content, str)
    assert "<context>" in rendered.content[0].text
    assert "origin=doc-1" in rendered.content[0].text
    assert "<request>\nAnswer this.\nattached\n</request>" in rendered.content[0].text


def test_context_references_survive_when_compaction_keeps_no_user_message() -> None:
    """References remain model-visible after the original User ModelStep is omitted."""
    assistant = AssistantMessage(tool_calls=(ToolCall(id="call-1", name="read_file", arguments="{}"),))
    result = ToolMessage(tool_call_id="call-1", content="observation")
    view = ContextView((ContextBlock(kind="task_state", origin="request", content="customer=Alpha"),))

    rendered = ContextRenderer().render_messages((assistant, result), input_messages=(), context_view=view)

    assert rendered[:2] == (assistant, result)
    assert isinstance(rendered[-1], UserMessage)
    assert rendered[-1].content == "<context>\n[reference kind=task_state origin=request]\ncustomer=Alpha\n</context>"


def test_context_renderer_preserves_every_current_input_and_labels_references_on_the_last() -> None:
    first = UserMessage(message_id="first", content="first request")
    second = UserMessage(message_id="second", content="supplement")
    view = ContextView((ContextBlock(kind="knowledge", origin="source", content="reference"),))

    rendered = ContextRenderer().render_messages((), input_messages=(first, second), context_view=view)

    assert rendered[0] == first
    assert tuple(value.message_id for value in rendered) == ("first", "second")
    assert rendered[-1].content != second.content
    assert second.content == "supplement"
