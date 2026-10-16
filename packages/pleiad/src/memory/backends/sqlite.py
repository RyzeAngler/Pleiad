import sqlite3
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from pleiad.memory.errors import (
    MemoryDataCorruptedError,
    MemoryStorageError,
    MemoryTurnConflictError,
    MemoryTurnInvalidError,
)
from pleiad.memory.turn import Turn
from pleiad.messages.message import MESSAGE_ADAPTER, Message, UserMessage


def read(database: Path, memory_id: str, after_turn: int) -> tuple[Turn, ...]:
    """Read completed Turns from SQLite after an exclusive sequence boundary."""
    if after_turn < 0:
        raise ValueError("after_turn must not be negative")
    try:
        with connect(database) as connection:
            initialize_schema(connection)
            turns = load_turns(connection, memory_id)
    except (OSError, sqlite3.Error) as error:
        raise MemoryStorageError(f"SQLite read failed for memory {memory_id}.") from error
    return tuple(turn for turn in turns if turn.completed and turn.sequence > after_turn)


def active(database: Path, memory_id: str) -> Turn | None:
    """Read the only active Turn from SQLite, if one exists."""
    try:
        with connect(database) as connection:
            initialize_schema(connection)
            turns = load_turns(connection, memory_id)
    except (OSError, sqlite3.Error) as error:
        raise MemoryStorageError(f"SQLite active read failed for memory {memory_id}.") from error
    for turn in turns:
        if not turn.completed:
            return turn
    return None


def begin(database: Path, memory_id: str, agent_id: str, messages: tuple[Message, ...]) -> Turn:
    """Create an active Turn in one SQLite transaction."""
    try:
        with connect(database) as connection:
            initialize_schema(connection)
            connection.execute("BEGIN IMMEDIATE")
            ensure_memory(connection, memory_id)
            turns = load_turns(connection, memory_id)
            current_sequence = next_sequence(connection, memory_id)
            if current_sequence != len(turns) + 1:
                raise MemoryDataCorruptedError("SQLite Turn sequence state is inconsistent.")
            if any(not turn.completed for turn in turns):
                raise MemoryTurnConflictError("A ShortMemory cannot begin a second active Turn.")
            if len(messages) > 1 or (messages and not isinstance(messages[0], UserMessage)):
                raise MemoryTurnInvalidError("A new Turn can begin with zero or one User message.")
            check_message_ids(turns, "", messages)
            try:
                turn = Turn(
                    turn_id=uuid4().hex,
                    sequence=current_sequence,
                    agent_id=agent_id,
                    messages=messages,
                )
            except ValidationError as error:
                raise MemoryTurnInvalidError("The initial messages break Turn ordering or pairing.") from error
            insert_turn(connection, memory_id, turn)
            connection.execute(
                "UPDATE short_memories SET next_turn_sequence = ? WHERE memory_id = ?",
                (current_sequence + 1, memory_id),
            )
            return turn
    except (OSError, sqlite3.Error) as error:
        raise MemoryStorageError(f"SQLite begin failed for memory {memory_id}.") from error


def append(database: Path, memory_id: str, turn_id: str, messages: tuple[Message, ...]) -> Turn:
    """Append messages to an active SQLite Turn in one transaction."""
    try:
        with connect(database) as connection:
            initialize_schema(connection)
            connection.execute("BEGIN IMMEDIATE")
            turns = load_turns(connection, memory_id)
            turn = find(turns, turn_id)[1]
            if turn is None:
                raise MemoryTurnConflictError("The requested Turn does not exist.")
            if turn.completed:
                raise MemoryTurnConflictError("A completed Turn cannot receive new messages.")
            check_message_ids(turns, turn_id, messages)
            updated = turn.append(messages)
            write_new_messages(connection, memory_id, turn, updated)
            return updated
    except (OSError, sqlite3.Error) as error:
        raise MemoryStorageError(f"SQLite append failed for memory {memory_id}.") from error


def complete(database: Path, memory_id: str, turn_id: str, messages: tuple[Message, ...]) -> Turn:
    """Append final messages and complete a SQLite Turn atomically."""
    try:
        with connect(database) as connection:
            initialize_schema(connection)
            connection.execute("BEGIN IMMEDIATE")
            turns = load_turns(connection, memory_id)
            turn = find(turns, turn_id)[1]
            if turn is None:
                raise MemoryTurnConflictError("The requested Turn does not exist.")
            check_message_ids(turns, turn_id, messages)
            updated = turn.complete(messages)
            write_new_messages(connection, memory_id, turn, updated)
            if not turn.completed and updated.completed:
                connection.execute("UPDATE turns SET completed = 1 WHERE turn_id = ?", (turn_id,))
            return updated
    except (OSError, sqlite3.Error) as error:
        raise MemoryStorageError(f"SQLite complete failed for memory {memory_id}.") from error


def abort(database: Path, memory_id: str, turn_id: str) -> Turn | None:
    """Remove an active SQLite Turn and restore its sequence allocation."""
    try:
        with connect(database) as connection:
            initialize_schema(connection)
            connection.execute("BEGIN IMMEDIATE")
            turns = load_turns(connection, memory_id)
            turn = find(turns, turn_id)[1]
            if turn is None:
                return None
            if turn.completed:
                raise MemoryTurnConflictError("A completed Turn cannot be aborted.")
            connection.execute("DELETE FROM messages WHERE turn_id = ?", (turn_id,))
            connection.execute("DELETE FROM turns WHERE turn_id = ?", (turn_id,))
            connection.execute(
                "UPDATE short_memories SET next_turn_sequence = ? WHERE memory_id = ?",
                (turn.sequence, memory_id),
            )
            return turn
    except (OSError, sqlite3.Error) as error:
        raise MemoryStorageError(f"SQLite abort failed for memory {memory_id}.") from error


def connect(database: Path) -> sqlite3.Connection:
    """Open one SQLite connection with foreign-key enforcement enabled."""
    connection = sqlite3.connect(str(database), timeout=30.0)
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize_schema(connection: sqlite3.Connection) -> None:
    """Create the ShortMemory schema when the database is first used."""
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS short_memories (
            memory_id TEXT PRIMARY KEY,
            next_turn_sequence INTEGER NOT NULL CHECK (next_turn_sequence >= 1)
        );
        CREATE TABLE IF NOT EXISTS turns (
            turn_id TEXT PRIMARY KEY,
            memory_id TEXT NOT NULL,
            sequence INTEGER NOT NULL CHECK (sequence >= 1),
            agent_id TEXT NOT NULL,
            completed INTEGER NOT NULL CHECK (completed IN (0, 1)),
            UNIQUE (memory_id, sequence),
            FOREIGN KEY (memory_id) REFERENCES short_memories(memory_id)
        );
        CREATE TABLE IF NOT EXISTS messages (
            memory_id TEXT NOT NULL,
            message_id TEXT NOT NULL,
            turn_id TEXT NOT NULL,
            sequence INTEGER NOT NULL CHECK (sequence >= 0),
            payload TEXT NOT NULL,
            PRIMARY KEY (memory_id, message_id),
            UNIQUE (turn_id, sequence),
            FOREIGN KEY (memory_id) REFERENCES short_memories(memory_id),
            FOREIGN KEY (turn_id) REFERENCES turns(turn_id)
        );
        """,
    )


def ensure_memory(connection: sqlite3.Connection, memory_id: str) -> None:
    """Create the SQLite memory row when it does not already exist."""
    connection.execute(
        "INSERT OR IGNORE INTO short_memories (memory_id, next_turn_sequence) VALUES (?, 1)",
        (memory_id,),
    )


def next_sequence(connection: sqlite3.Connection, memory_id: str) -> int:
    """Return the next allocated Turn sequence for one SQLite memory."""
    row = connection.execute(
        "SELECT next_turn_sequence FROM short_memories WHERE memory_id = ?",
        (memory_id,),
    ).fetchone()
    if row is None:
        raise MemoryDataCorruptedError("SQLite memory row is missing after initialization.")
    sequence = row[0]
    if not isinstance(sequence, int) or sequence < 1:
        raise MemoryDataCorruptedError("SQLite next Turn sequence is invalid.")
    return sequence


def load_turns(connection: sqlite3.Connection, memory_id: str) -> tuple[Turn, ...]:
    """Load and validate every stored Turn for one memory ID."""
    memory_row = connection.execute(
        "SELECT next_turn_sequence FROM short_memories WHERE memory_id = ?",
        (memory_id,),
    ).fetchone()
    turn_rows = connection.execute(
        "SELECT turn_id, sequence, agent_id, completed FROM turns WHERE memory_id = ? ORDER BY sequence",
        (memory_id,),
    ).fetchall()
    if memory_row is None:
        if turn_rows:
            raise MemoryDataCorruptedError("SQLite Turns exist without their memory row.")
        return ()
    turns: list[Turn] = []
    for turn_id, sequence, agent_id, completed in turn_rows:
        if completed not in (0, 1):
            raise MemoryDataCorruptedError("SQLite Turn completion value is invalid.")
        message_rows = connection.execute(
            "SELECT sequence, payload FROM messages WHERE memory_id = ? AND turn_id = ? ORDER BY sequence",
            (memory_id, turn_id),
        ).fetchall()
        try:
            message_sequences = tuple(row[0] for row in message_rows)
            if message_sequences != tuple(range(len(message_rows))):
                raise MemoryDataCorruptedError("SQLite Message sequences are not continuous.")
            messages = tuple(MESSAGE_ADAPTER.validate_json(row[1]) for row in message_rows)
            turns.append(
                Turn(
                    turn_id=turn_id,
                    sequence=sequence,
                    agent_id=agent_id,
                    completed=bool(completed),
                    messages=messages,
                ),
            )
        except (TypeError, ValueError, ValidationError) as error:
            raise MemoryDataCorruptedError("SQLite stored Message or Turn data is invalid.") from error
    sequences = tuple(turn.sequence for turn in turns)
    if sequences != tuple(range(1, len(turns) + 1)):
        raise MemoryDataCorruptedError("SQLite Turn sequences are not continuous.")
    message_ids = tuple(message.message_id for turn in turns for message in turn.messages)
    if len(message_ids) != len(set(message_ids)):
        raise MemoryDataCorruptedError("SQLite Message IDs are not unique across Turns.")
    active_turns = tuple(turn for turn in turns if not turn.completed)
    if len(active_turns) > 1 or (active_turns and active_turns[0] is not turns[-1]):
        raise MemoryDataCorruptedError("SQLite ShortMemory has an invalid active Turn.")
    expected_next = len(turns) + 1
    if memory_row[0] != expected_next:
        raise MemoryDataCorruptedError("SQLite next Turn sequence does not match stored Turns.")
    return tuple(turns)


def insert_turn(connection: sqlite3.Connection, memory_id: str, turn: Turn) -> None:
    """Insert one validated Turn and all of its Messages."""
    connection.execute(
        "INSERT INTO turns (turn_id, memory_id, sequence, agent_id, completed) VALUES (?, ?, ?, ?, ?)",
        (turn.turn_id, memory_id, turn.sequence, turn.agent_id, int(turn.completed)),
    )
    for sequence, message in enumerate(turn.messages):
        connection.execute(
            "INSERT INTO messages (memory_id, message_id, turn_id, sequence, payload) VALUES (?, ?, ?, ?, ?)",
            (memory_id, message.message_id, turn.turn_id, sequence, message.model_dump_json()),
        )


def write_new_messages(
    connection: sqlite3.Connection,
    memory_id: str,
    previous: Turn,
    updated: Turn,
) -> None:
    """Persist only the suffix newly appended to an existing Turn."""
    new_messages = updated.messages[len(previous.messages) :]
    for sequence, message in enumerate(new_messages, start=len(previous.messages)):
        connection.execute(
            "INSERT INTO messages (memory_id, message_id, turn_id, sequence, payload) VALUES (?, ?, ?, ?, ?)",
            (memory_id, message.message_id, updated.turn_id, sequence, message.model_dump_json()),
        )


def find(turns: tuple[Turn, ...], turn_id: str) -> tuple[int, Turn | None]:
    """Find a Turn by ID without fabricating a missing value."""
    for index, turn in enumerate(turns):
        if turn.turn_id == turn_id:
            return index, turn
    return -1, None


def check_message_ids(turns: tuple[Turn, ...], turn_id: str, messages: tuple[Message, ...]) -> None:
    """Reject a message ID that belongs to another Turn in the same memory."""
    other_ids = {message.message_id for turn in turns if turn.turn_id != turn_id for message in turn.messages}
    if any(message.message_id in other_ids for message in messages):
        raise MemoryTurnConflictError("A message ID already belongs to another Turn.")
