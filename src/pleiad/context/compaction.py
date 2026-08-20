"""Conservative deterministic Context compaction."""

import json
from dataclasses import dataclass
from uuid import uuid4

from pleiad.context.budget import BudgetGuard, BudgetReport
from pleiad.context.contracts import ContextCheckpoint
from pleiad.llm.contracts import LLMRequest, Message, MessageRole


@dataclass(frozen=True, slots=True)
class CompactionResult:
    """Result of reducing old Tool Result bodies in one request."""

    messages: tuple[Message, ...]
    checkpoint: ContextCheckpoint | None
    report: BudgetReport


class ContextCompactor:
    """Free safe space without inventing facts or breaking Tool relations."""

    retained_message_count = 8

    def __init__(self, budget: BudgetGuard | None = None) -> None:
        self.budget = budget or BudgetGuard()

    def compact(
        self,
        request: LLMRequest,
        *,
        conversation_id: str,
        execution_id: str,
        covered_until_sequence: int,
        raw_query: str | None = None,
        prior_handoff: str | None = None,
    ) -> CompactionResult:
        """Replace old Tool Result bodies until the low watermark is reached."""
        before = self.budget.check(request)
        messages = list(request.messages)
        candidates = [
            index
            for index, message in enumerate(messages)
            if message.role is MessageRole.TOOL and index < max(1, len(messages) - 8)
        ]
        changed = False
        for index in candidates:
            if before.estimated_tokens <= before.low_watermark_tokens:
                break
            message = messages[index]
            messages[index] = message.model_copy(
                update={
                    "content": json.dumps(
                        {
                            "compacted": True,
                            "tool_call_id": message.tool_call_id,
                            "note": (
                                "The full Tool Result remains in the durable session journal "
                                "and was removed from the active Context."
                            ),
                        },
                        ensure_ascii=False,
                    )
                }
            )
            changed = True
            candidate_request = request.model_copy(update={"messages": tuple(messages)})
            before = self.budget.check(candidate_request)

        compacted_request = request.model_copy(update={"messages": tuple(messages)})
        after = self.budget.check(compacted_request)
        checkpoint = None
        if changed:
            checkpoint = ContextCheckpoint(
                checkpoint_id=uuid4().hex,
                conversation_id=conversation_id,
                execution_id=execution_id,
                covered_until_sequence=covered_until_sequence,
                input_tokens_before=self.budget.check(request).estimated_tokens,
                input_tokens_after=after.estimated_tokens,
                handoff=_handoff(
                    raw_query=raw_query,
                    prior_handoff=prior_handoff,
                    messages=messages,
                ),
            )
        return CompactionResult(tuple(messages), checkpoint, after)


def _handoff(
    *,
    raw_query: str | None,
    prior_handoff: str | None,
    messages: list[Message],
) -> str:
    """Build a bounded operational handoff without serializing the dynamic Prompt."""
    recent: list[dict[str, object]] = []
    for message in messages[-ContextCompactor.retained_message_count :]:
        if message.role is MessageRole.ASSISTANT:
            recent.append(
                {
                    "role": message.role.value,
                    "content": _truncate(message.content),
                    "tool_names": [call.name for call in message.tool_calls],
                }
            )
        elif message.role is MessageRole.TOOL:
            recent.append(
                {
                    "role": message.role.value,
                    "name": message.name,
                    "content": _truncate(message.content, limit=600),
                }
            )
    return json.dumps(
        {
            "objective": raw_query,
            "prior_handoff": prior_handoff,
            "recent_state": recent,
            "note": (
                "Older request context was compacted. System rules, permissions, task state, "
                "and artifact existence must be reloaded from their authoritative sources."
            ),
        },
        ensure_ascii=False,
    )


def _truncate(value: str | None, *, limit: int = 800) -> str:
    """Keep checkpoint state bounded while retaining readable trailing context."""
    if not value:
        return ""
    return value if len(value) <= limit else f"{value[:limit]}…"
