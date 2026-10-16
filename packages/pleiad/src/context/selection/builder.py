import logging
from typing import ClassVar

from pleiad.context.selection.models import ContextBlock, ContextView

logger = logging.getLogger(__name__)


class ContextBuilder:
    """Select and order caller-provided dynamic reference blocks deterministically."""

    kind_order: ClassVar[dict[str, int]] = {
        "task_state": 0,
        "memory": 1,
        "knowledge": 2,
        "file": 3,
        "artifact": 4,
    }

    def build(self, context_blocks: tuple[ContextBlock, ...] = ()) -> ContextView:
        """Return a stable immutable view without promoting data to instructions.

        Args:
            context_blocks: Already authorized reference data from the caller.

        Returns:
            Blocks ordered by the SDK's fixed source policy.
        """
        ordered = tuple(
            block
            for index, block in sorted(
                enumerate(context_blocks),
                key=lambda item: self.sort_key(item[1], item[0]),
            )
        )
        logger.debug(f"context blocks selected candidates={len(context_blocks)} selected={len(ordered)}")
        return ContextView(blocks=ordered)

    @classmethod
    def sort_key(cls, block: ContextBlock, index: int) -> tuple[int, float, str, str, int]:
        """Return the stable key used for one candidate block."""
        relevance = block.relevance if block.relevance is not None else float("-inf")
        return (cls.kind_order.get(block.kind, 5), -relevance, block.kind, block.origin, index)
