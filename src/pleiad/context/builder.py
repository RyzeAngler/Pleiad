"""Select structured dynamic Context without building the final Prompt."""

from pleiad.context.contracts import ContextBlock, ContextBundle, ContextRequest


class ContextBuilder:
    """Build request-local Context from explicit Runtime inputs."""

    def build(self, request: ContextRequest) -> ContextBundle:
        """Project bounded profile and checkpoint data into Context blocks."""
        profile_blocks = tuple(
            ContextBlock(
                source_type="memory.profile",
                source_id=item.memory_id,
                content=item.content,
                score=item.score,
                metadata={
                    "kind": item.kind.value,
                    "scope": item.scope.value,
                    "source_message_ids": list(item.source_message_ids),
                    **item.metadata,
                },
            )
            for item in request.personal_profile
        )
        checkpoint_blocks = (
            (
                ContextBlock(
                    source_type="context.checkpoint",
                    source_id=f"checkpoint:{request.execution_id}",
                    content=request.checkpoint_handoff,
                ),
            )
            if request.checkpoint_handoff
            else ()
        )
        return ContextBundle(
            blocks=(*checkpoint_blocks, *profile_blocks),
            metadata={
                "agent_id": request.agent_id,
                "conversation_id": request.conversation_id,
                "execution_id": request.execution_id,
            },
        )
