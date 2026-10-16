import asyncio

import pytest

from pleiad.memory.errors import MemoryTurnConflictError, MemoryTurnInvalidError
from pleiad.memory.short_memory import ShortMemory
from pleiad.messages.message import AssistantMessage, Message, ToolCall, ToolMessage, UserMessage


def test_in_memory_read_hides_an_active_turn_until_it_is_completed() -> None:
    """Only completed Turns are returned by the normal ShortMemory read."""

    async def scenario() -> tuple[tuple[int, ...], tuple[int, ...]]:
        memory = ShortMemory.in_memory("chat")
        turn = await memory.begin("agent", (UserMessage(message_id="user-1", content="question"),))
        before = tuple(item.sequence for item in await memory.read())
        await memory.complete(turn.turn_id, (AssistantMessage(message_id="assistant-1", content="answer"),))
        after = tuple(item.sequence for item in await memory.read())
        return before, after

    assert asyncio.run(scenario()) == ((), (1,))


def test_in_memory_rejects_two_active_turns_in_one_stream() -> None:
    """A second model call cannot begin before the active Turn is closed."""

    async def scenario() -> None:
        memory = ShortMemory.in_memory("chat")
        await memory.begin("agent")
        with pytest.raises(MemoryTurnConflictError, match="second active Turn"):
            await memory.begin("agent")

    asyncio.run(scenario())


def test_in_memory_begin_rejects_non_user_initial_messages() -> None:
    """A new Turn accepts only its optional User input before the model call."""

    async def scenario() -> None:
        memory = ShortMemory.in_memory("chat")
        with pytest.raises(MemoryTurnInvalidError, match="zero or one User"):
            await memory.begin("agent", (AssistantMessage(message_id="assistant-1", content="answer"),))

    asyncio.run(scenario())


def test_in_memory_preserves_tool_call_and_result_order() -> None:
    """ShortMemory keeps Tool messages in the order required by the model."""

    async def scenario() -> tuple[str, ...]:
        memory = ShortMemory.in_memory("chat")
        turn = await memory.begin("agent", (UserMessage(message_id="user-1", content="question"),))
        await memory.append(
            turn.turn_id,
            (
                AssistantMessage(
                    message_id="assistant-1",
                    tool_calls=(ToolCall(id="call-1", name="lookup", arguments="{}"),),
                ),
            ),
        )
        await memory.complete(
            turn.turn_id,
            (
                ToolMessage(
                    message_id="result-message-1",
                    tool_call_id="call-1",
                    content='{"ok": true}',
                ),
            ),
        )
        return tuple(item.role for item in (await memory.read())[0].messages)

    assert asyncio.run(scenario()) == (
        "user",
        "assistant",
        "tool",
    )


def test_in_memory_retries_an_append_without_duplicate_messages() -> None:
    """A repeated identical append is idempotent in the in-memory backend."""

    async def scenario() -> int:
        memory = ShortMemory.in_memory("chat")
        turn = await memory.begin("agent")
        assistant: Message = AssistantMessage(message_id="assistant-1", content="answer")
        await memory.append(turn.turn_id, (assistant,))
        updated = await memory.append(turn.turn_id, (assistant,))
        return len(updated.messages)

    assert asyncio.run(scenario()) == 1


def test_in_memory_abort_releases_the_uncommitted_turn_sequence() -> None:
    """Aborting the active Turn leaves completed history unchanged and reusable."""

    async def scenario() -> tuple[int, tuple[int, ...]]:
        memory = ShortMemory.in_memory("chat")
        turn = await memory.begin("agent")
        await memory.abort(turn.turn_id)
        replacement = await memory.begin("agent")
        return replacement.sequence, tuple(item.sequence for item in await memory.read())

    assert asyncio.run(scenario()) == (1, ())
