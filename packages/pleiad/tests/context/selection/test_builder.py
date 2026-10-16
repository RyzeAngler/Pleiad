from pleiad.context.selection.builder import ContextBuilder
from pleiad.context.selection.models import ContextBlock


def test_context_builder_orders_sources_and_relevance_deterministically() -> None:
    """Context selection follows source order, then relevance and origin."""
    blocks = (
        ContextBlock(kind="knowledge", origin="z", content="z", relevance=1),
        ContextBlock(kind="memory", origin="b", content="b", relevance=1),
        ContextBlock(kind="memory", origin="a", content="a", relevance=1),
        ContextBlock(kind="task_state", origin="task", content="task"),
    )

    view = ContextBuilder().build(blocks)

    assert [(block.kind, block.origin) for block in view.blocks] == [
        ("task_state", "task"),
        ("memory", "a"),
        ("memory", "b"),
        ("knowledge", "z"),
    ]
