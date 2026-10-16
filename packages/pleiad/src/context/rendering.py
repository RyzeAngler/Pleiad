import logging

from pleiad.context.selection.models import ContextView
from pleiad.messages.message import TextBlock, UserMessage

logger = logging.getLogger(__name__)


class ContextRenderer:
    """Render dynamic references into a request-local User message."""

    def render(self, user_message: UserMessage, *, context_view: ContextView) -> UserMessage:
        """Return a temporary User message that preserves the original input fields.

        Args:
            user_message: Original User message that will remain in ShortMemory.
            context_view: Dynamic, untrusted reference data for this request.

        Returns:
            A model-only User message with references clearly separated from the request.

        Raises:
            ValueError: If the supplied message is not a User message.
        """
        if not context_view.blocks:
            return user_message
        references = "\n\n".join(
            f"[reference kind={block.kind} origin={block.origin}]\n{block.content}" for block in context_view.blocks
        )
        if isinstance(user_message.content, str):
            request_content = user_message.content
        else:
            request_content = "\n".join(block.text for block in user_message.content)
        rendered = f"<context>\n{references}\n</context>\n\n<request>\n{request_content}\n</request>"
        logger.debug(f"context rendered blocks={len(context_view.blocks)}")
        return user_message.model_copy(update={"content": (TextBlock(text=rendered),)})
