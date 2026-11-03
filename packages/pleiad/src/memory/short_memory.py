import asyncio
import hashlib
import logging
from pathlib import Path
from typing import Self

from pleiad.errors.base import PleiadError
from pleiad.memory.backends import in_memory, sqlite
from pleiad.memory.turn import Turn
from pleiad.messages.message import AssistantMessage, Message, ToolMessage

logger = logging.getLogger(__name__)


class ShortMemory:
    """Maintain one ordered stream of model Turns."""

    def __init__(self, memory_id: str, database: Path | None = None) -> None:
        """Create a memory handle backed by memory or SQLite.

        Args:
            memory_id: Stable caller-owned identity for the model interaction stream.
            database: SQLite database path, or ``None`` for process-local storage.

        Raises:
            ValueError: If the memory ID is empty.
        """
        if not memory_id.strip():
            raise ValueError("memory_id must be non-empty")
        self.memory_id = memory_id
        self.database = database
        self.turns: tuple[Turn, ...] = ()
        self.lock = asyncio.Lock()
        self.memory_key = hashlib.sha256(memory_id.encode("utf-8")).hexdigest()[:12]

    @classmethod
    def in_memory(cls, memory_id: str) -> Self:
        """Create a process-local ShortMemory.

        Args:
            memory_id: Stable caller-owned identity for the memory stream.

        Returns:
            A new in-memory ShortMemory handle.
        """
        return cls(memory_id)

    @classmethod
    def sqlite(cls, memory_id: str, database: Path) -> Self:
        """Create a ShortMemory persisted in SQLite.

        Args:
            memory_id: Stable caller-owned identity for the memory stream.
            database: SQLite database path.

        Returns:
            A SQLite-backed ShortMemory handle.
        """
        return cls(memory_id, database=database)

    async def read(self, *, after_turn: int = 0) -> tuple[Turn, ...]:
        """Read completed Turns after an exclusive sequence boundary.

        Args:
            after_turn: Return only Turns with a larger sequence.

        Returns:
            Completed Turns in ascending sequence order.

        Raises:
            ValueError: If ``after_turn`` is negative.
        """
        async with self.lock:
            try:
                if self.database is None:
                    result = in_memory.read(self.turns, after_turn)
                else:
                    result = await asyncio.to_thread(sqlite.read, self.database, self.memory_id, after_turn)
            except PleiadError as error:
                logger.error(f"memory read failed key={self.memory_key} code={error.code}", exc_info=True)
                raise
        logger.debug(f"memory read completed key={self.memory_key} returned={len(result)}")
        return result

    async def active(self) -> Turn | None:
        """Return the only active Turn, if the memory has one."""
        async with self.lock:
            try:
                if self.database is None:
                    result = in_memory.active(self.turns)
                else:
                    result = await asyncio.to_thread(sqlite.active, self.database, self.memory_id)
            except PleiadError as error:
                logger.error(f"memory active read failed key={self.memory_key} code={error.code}", exc_info=True)
                raise
        turn_key = "none" if result is None else result.turn_id
        logger.debug(f"memory active read completed key={self.memory_key} turn={turn_key}")
        return result

    async def read_tool_result(self, message_id: str) -> ToolMessage | None:
        """Read an original Tool observation from completed history in this scope.

        Args:
            message_id: Stable identity published in a compacted result reference.

        Returns:
            The original Tool message, or None if no completed result has that ID.

        Raises:
            ValueError: If the message identity is empty.
        """
        if not message_id:
            raise ValueError("message_id must not be empty")
        for turn in await self.read():
            for message in turn.messages:
                if message.message_id == message_id and isinstance(message, ToolMessage):
                    return message
        return None

    async def begin(self, agent_id: str, messages: tuple[Message, ...] = ()) -> Turn:
        """Create the next active Turn.

        Args:
            agent_id: Agent identity associated with the model call.
            messages: Initial messages, normally zero or one User message.

        Returns:
            The newly created active Turn.
        """
        async with self.lock:
            try:
                if self.database is None:
                    state, turn = in_memory.begin(self.turns, agent_id, messages)
                    self.turns = state
                else:
                    turn = await asyncio.to_thread(sqlite.begin, self.database, self.memory_id, agent_id, messages)
            except PleiadError as error:
                logger.error(f"memory turn begin failed key={self.memory_key} code={error.code}", exc_info=True)
                raise
        logger.debug(
            f"memory turn begun key={self.memory_key} turn={turn.turn_id} "
            f"sequence={turn.sequence} agent={turn.agent_id}"
        )
        return turn

    async def append(self, turn_id: str, messages: tuple[Message, ...]) -> Turn:
        """Append an ordered batch to an active Turn.

        Args:
            turn_id: ID of the active Turn to update.
            messages: Non-empty ordered Message batch.

        Returns:
            The updated active Turn.
        """
        async with self.lock:
            try:
                if self.database is None:
                    state, turn = in_memory.append(self.turns, turn_id, messages)
                    self.turns = state
                else:
                    turn = await asyncio.to_thread(sqlite.append, self.database, self.memory_id, turn_id, messages)
            except PleiadError as error:
                logger.error(f"memory turn append failed key={self.memory_key} code={error.code}", exc_info=True)
                raise
        logger.debug(f"memory turn appended key={self.memory_key} turn={turn_id} messages={len(messages)}")
        return turn

    async def complete(self, turn_id: str, messages: tuple[Message, ...] = ()) -> Turn:
        """Append final messages and close an active Turn atomically.

        Args:
            turn_id: ID of the Turn to complete.
            messages: Optional final batch, normally Assistant or Tool Result messages.

        Returns:
            The completed Turn.
        """
        async with self.lock:
            try:
                if self.database is None:
                    state, turn = in_memory.complete(self.turns, turn_id, messages)
                    self.turns = state
                else:
                    turn = await asyncio.to_thread(sqlite.complete, self.database, self.memory_id, turn_id, messages)
            except PleiadError as error:
                logger.error(f"memory turn complete failed key={self.memory_key} code={error.code}", exc_info=True)
                raise
        tool_call_count = sum(
            len(message.tool_calls) for message in turn.messages if isinstance(message, AssistantMessage)
        )
        logger.debug(
            f"memory turn completed key={self.memory_key} turn={turn_id} "
            f"messages={len(turn.messages)} tool_calls={tool_call_count}"
        )
        return turn

    async def abort(self, turn_id: str) -> None:
        """Abort an active Turn without changing completed history.

        Args:
            turn_id: ID of the active Turn to remove.
        """
        async with self.lock:
            try:
                if self.database is None:
                    state, removed = in_memory.abort(self.turns, turn_id)
                    self.turns = state
                else:
                    removed = await asyncio.to_thread(sqlite.abort, self.database, self.memory_id, turn_id)
            except PleiadError as error:
                logger.error(f"memory turn abort failed key={self.memory_key} code={error.code}", exc_info=True)
                raise
        turn_key = "none" if removed is None else removed.turn_id
        logger.debug(f"memory turn aborted key={self.memory_key} turn={turn_key}")
