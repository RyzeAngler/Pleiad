from pleiad.context.rendering import ContextRenderer
from pleiad.context.selection.models import ContextBlock, ContextView
from pleiad.messages.message import TextBlock, UserMessage


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
