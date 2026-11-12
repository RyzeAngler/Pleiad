import logging

from pleiad.context.selection.models import ContextView
from pleiad.messages.images import ImageBlock
from pleiad.messages.message import Message, TextBlock, UserMessage

logger = logging.getLogger(__name__)


class ContextRenderer:
    """Render dynamic references into a request-local User message."""

    def render(self, user_message: UserMessage, *, context_view: ContextView) -> UserMessage:
        """Return a temporary User message that preserves the original input fields.

        Args:
            user_message: Original User message that will remain in the Session journal.
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
        content: tuple[TextBlock | ImageBlock, ...]
        if isinstance(user_message.content, str):
            rendered = f"<context>\n{references}\n</context>\n\n<request>\n{user_message.content}\n</request>"
            content = (TextBlock(text=rendered),)
        else:
            content = (TextBlock(text=f"<context>\n{references}\n</context>"), *user_message.content)
        logger.debug(f"context rendered blocks={len(context_view.blocks)}")
        return user_message.model_copy(update={"content": content})

    def render_messages(
        self,
        messages: tuple[Message, ...],
        *,
        input_messages: tuple[UserMessage, ...],
        context_view: ContextView,
    ) -> tuple[Message, ...]:
        """Render references in the current request without changing stored history.

        Args:
            messages: Retained completed history in its original order.
            input_messages: Ordered current inputs, empty for continuation requests.
            context_view: References selected for this model request.

        Returns:
            The request messages with references beside the latest User message.
            If compaction retained no User message, a reference-only User message
            follows the retained history.
        """
        request = (*messages, *input_messages)
        if not context_view.blocks:
            return request
        for index in range(len(request) - 1, -1, -1):
            candidate = request[index]
            if isinstance(candidate, UserMessage):
                rendered = self.render(candidate, context_view=context_view)
                return (*request[:index], rendered, *request[index + 1 :])
        references = "\n\n".join(
            f"[reference kind={block.kind} origin={block.origin}]\n{block.content}" for block in context_view.blocks
        )
        return (*request, UserMessage(content=f"<context>\n{references}\n</context>"))
