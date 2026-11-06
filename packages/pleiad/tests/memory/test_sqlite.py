import asyncio
import sqlite3
from pathlib import Path

import pytest

from pleiad.memory.errors import (
    MemoryDataCorruptedError,
    MemoryStorageError,
    MemoryTurnConflictError,
    MemoryTurnInvalidError,
)
from pleiad.memory.short_memory import ShortMemory
from pleiad.messages.message import AssistantMessage, ToolCall, ToolMessage, UserMessage


def test_sqlite_reopens_with_the_same_completed_turn_order(tmp_path: Path) -> None:
    """A new ShortMemory handle restores completed Turn and Message order."""
    database = tmp_path / "memory.db"

    async def scenario() -> tuple[tuple[int, ...], tuple[str, ...]]:
        first = ShortMemory.sqlite("chat", database)
        turn = await first.begin("agent", (UserMessage(message_id="user-1", content="question"),))
        await first.complete(turn.turn_id, (AssistantMessage(message_id="assistant-1", content="answer"),))
        restored = ShortMemory.sqlite("chat", database)
        turns = await restored.read()
        return tuple(item.sequence for item in turns), tuple(item.role for item in turns[0].messages)

    assert asyncio.run(scenario()) == ((1,), ("user", "assistant"))


def test_sqlite_completion_failure_does_not_write_a_partial_batch(tmp_path: Path) -> None:
    """SQLite rolls back final Messages when Turn completion validation fails."""
    database = tmp_path / "memory.db"

    async def scenario() -> tuple[bool, tuple[str, ...]]:
        memory = ShortMemory.sqlite("chat", database)
        turn = await memory.begin("agent", (UserMessage(message_id="user-1", content="question"),))
        assistant = AssistantMessage(
            message_id="assistant-1",
            tool_calls=(ToolCall(id="call-1", name="lookup", arguments="{}"),),
        )
        with pytest.raises(MemoryTurnInvalidError, match="cannot be completed"):
            await memory.complete(turn.turn_id, (assistant,))
        active = await memory.active()
        if active is None:
            raise AssertionError("the active Turn must remain after a failed transaction")
        return active.completed, tuple(item.role for item in active.messages)

    assert asyncio.run(scenario()) == (False, ("user",))


def test_sqlite_reports_corrupt_message_payload_instead_of_skipping_it(tmp_path: Path) -> None:
    """Invalid persisted JSON is a system failure, never an empty history."""
    database = tmp_path / "memory.db"

    async def create_turn() -> None:
        memory = ShortMemory.sqlite("chat", database)
        turn = await memory.begin("agent", (UserMessage(message_id="user-1", content="question"),))
        await memory.complete(turn.turn_id, (AssistantMessage(message_id="assistant-1", content="answer"),))

    asyncio.run(create_turn())
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE messages SET payload = ?", ("not-json",))

    async def read_corrupt_data() -> None:
        with pytest.raises(MemoryDataCorruptedError, match="stored Message"):
            await ShortMemory.sqlite("chat", database).read()

    asyncio.run(read_corrupt_data())


def test_sqlite_reports_a_message_sequence_gap_instead_of_reordering_it(tmp_path: Path) -> None:
    """A persisted Message sequence gap is a corruption failure."""
    database = tmp_path / "memory.db"

    async def create_turn() -> None:
        memory = ShortMemory.sqlite("chat", database)
        turn = await memory.begin("agent", (UserMessage(message_id="user-1", content="question"),))
        await memory.complete(turn.turn_id, (AssistantMessage(message_id="assistant-1", content="answer"),))

    asyncio.run(create_turn())
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE messages SET sequence = 4 WHERE message_id = ?", ("assistant-1",))

    async def read_corrupt_data() -> None:
        with pytest.raises(MemoryDataCorruptedError, match="Message sequences"):
            await ShortMemory.sqlite("chat", database).read()

    asyncio.run(read_corrupt_data())


def test_sqlite_serializes_concurrent_begin_operations(tmp_path: Path) -> None:
    """Two SQLite handles cannot create two active Turns for one memory ID."""
    database = tmp_path / "memory.db"
    first = ShortMemory.sqlite("chat", database)
    second = ShortMemory.sqlite("chat", database)

    async def begin(memory: ShortMemory) -> str:
        try:
            await memory.begin("agent")
        except MemoryTurnConflictError:
            return "conflict"
        return "created"

    async def scenario() -> tuple[str, str]:
        return await asyncio.gather(begin(first), begin(second))

    assert sorted(asyncio.run(scenario())) == ["conflict", "created"]


def test_sqlite_clear_removes_history_and_tool_results_across_reopened_handles(tmp_path: Path) -> None:
    """Deleted history stays absent after reopening without affecting another scope."""
    database = tmp_path / "memory.db"

    async def scenario() -> None:
        memory = ShortMemory.sqlite("chat", database)
        other = ShortMemory.sqlite("other-chat", database)
        question = UserMessage(message_id="user-1", content="question")
        assistant = AssistantMessage(
            message_id="assistant-1",
            tool_calls=(ToolCall(id="call-1", name="lookup", arguments="{}"),),
        )
        result = ToolMessage(message_id="result-1", tool_call_id="call-1", content="large result " * 10000)
        other_turn = await other.begin("agent", (question,))
        other_completed = await other.complete(other_turn.turn_id, (assistant, result))
        await memory.clear()
        turn = await memory.begin("agent", (question,))
        await memory.complete(turn.turn_id, (assistant, result))
        await memory.begin("agent")
        assert await memory.read_tool_result(result.message_id) == result

        await memory.clear()
        await memory.clear()

        restored = ShortMemory.sqlite("chat", database)
        assert await restored.read() == ()
        assert await restored.active() is None
        assert await restored.read_tool_result(result.message_id) is None
        assert await other.read() == (other_completed,)
        assert await other.read_tool_result(result.message_id) == result
        replacement = await restored.begin("agent", (question,))
        assert replacement.sequence == 1
        completed = await restored.complete(replacement.turn_id, (assistant, result))
        assert await memory.read() == (completed,)

    asyncio.run(scenario())


def test_sqlite_clear_failure_preserves_all_history_and_tool_results(tmp_path: Path) -> None:
    """A database failure after deleting messages rolls back the whole clear."""
    database = tmp_path / "memory.db"

    async def scenario() -> None:
        memory = ShortMemory.sqlite("chat", database)
        turn = await memory.begin("agent", (UserMessage(message_id="user-1", content="question"),))
        result = ToolMessage(message_id="result-1", tool_call_id="call-1", content="tool output")
        completed = await memory.complete(
            turn.turn_id,
            (
                AssistantMessage(
                    message_id="assistant-1",
                    tool_calls=(ToolCall(id="call-1", name="lookup", arguments="{}"),),
                ),
                result,
            ),
        )
        active = await memory.begin("agent")
        with sqlite3.connect(database) as connection:
            connection.execute(
                "CREATE TRIGGER fail_clear BEFORE DELETE ON turns "
                "BEGIN SELECT RAISE(ABORT, 'injected delete failure'); END",
            )

        with pytest.raises(MemoryStorageError, match="SQLite clear failed") as failure:
            await memory.clear()

        assert isinstance(failure.value.__cause__, sqlite3.IntegrityError)
        restored = ShortMemory.sqlite("chat", database)
        assert await restored.read() == (completed,)
        assert await restored.active() == active
        assert await restored.read_tool_result(result.message_id) == result

    asyncio.run(scenario())
