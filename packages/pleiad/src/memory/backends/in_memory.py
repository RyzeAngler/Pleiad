from uuid import uuid4

from pydantic import ValidationError

from pleiad.memory.errors import MemoryDataCorruptedError, MemoryTurnConflictError, MemoryTurnInvalidError
from pleiad.memory.turn import Turn
from pleiad.messages.message import Message, UserMessage


def validate_state(turns: tuple[Turn, ...]) -> None:
    """Validate the ordered state held by one in-memory ShortMemory."""
    sequences = tuple(turn.sequence for turn in turns)
    if sequences != tuple(range(1, len(turns) + 1)):
        raise MemoryDataCorruptedError("In-memory Turn sequences are not continuous.")
    message_ids = tuple(message.message_id for turn in turns for message in turn.messages)
    if len(message_ids) != len(set(message_ids)):
        raise MemoryDataCorruptedError("In-memory Message IDs are not unique across Turns.")
    active = tuple(turn for turn in turns if not turn.completed)
    if len(active) > 1 or (active and active[0] is not turns[-1]):
        raise MemoryDataCorruptedError("In-memory ShortMemory has an invalid active Turn.")


def read(turns: tuple[Turn, ...], after_turn: int) -> tuple[Turn, ...]:
    """Return completed Turns after an exclusive sequence boundary."""
    validate_state(turns)
    if after_turn < 0:
        raise ValueError("after_turn must not be negative")
    return tuple(turn for turn in turns if turn.completed and turn.sequence > after_turn)


def active(turns: tuple[Turn, ...]) -> Turn | None:
    """Return the only active Turn, if one exists."""
    validate_state(turns)
    for turn in turns:
        if not turn.completed:
            return turn
    return None


def begin(
    turns: tuple[Turn, ...],
    agent_id: str,
    messages: tuple[Message, ...],
) -> tuple[tuple[Turn, ...], Turn]:
    """Create an active Turn after the current sequence.

    Args:
        turns: Existing ordered Turns.
        agent_id: Agent identity for the new Turn.
        messages: Initial messages, normally zero or one User message.

    Returns:
        The updated state and newly created Turn.

    Raises:
        MemoryTurnConflictError: If another Turn is active.
        MemoryTurnInvalidError: If the initial messages violate the Turn contract.
    """
    validate_state(turns)
    if active(turns) is not None:
        raise MemoryTurnConflictError("A ShortMemory cannot begin a second active Turn.")
    if len(messages) > 1 or (messages and not isinstance(messages[0], UserMessage)):
        raise MemoryTurnInvalidError("A new Turn can begin with zero or one User message.")
    check_message_ids(turns, "", messages)
    try:
        turn = Turn(
            turn_id=uuid4().hex,
            sequence=len(turns) + 1,
            agent_id=agent_id,
            messages=messages,
        )
    except ValidationError as error:
        raise MemoryTurnInvalidError("The initial messages break Turn ordering or pairing.") from error
    return (*turns, turn), turn


def append(
    turns: tuple[Turn, ...],
    turn_id: str,
    messages: tuple[Message, ...],
) -> tuple[tuple[Turn, ...], Turn]:
    """Append messages to the selected active Turn."""
    validate_state(turns)
    index, turn = find(turns, turn_id)
    if turn is None:
        raise MemoryTurnConflictError("The requested Turn does not exist.")
    if turn.completed:
        raise MemoryTurnConflictError("A completed Turn cannot receive new messages.")
    check_message_ids(turns, turn_id, messages)
    updated = turn.append(messages)
    return replace(turns, index, updated), updated


def complete(
    turns: tuple[Turn, ...],
    turn_id: str,
    messages: tuple[Message, ...],
) -> tuple[tuple[Turn, ...], Turn]:
    """Append final messages and complete the selected Turn atomically."""
    validate_state(turns)
    index, turn = find(turns, turn_id)
    if turn is None:
        raise MemoryTurnConflictError("The requested Turn does not exist.")
    check_message_ids(turns, turn_id, messages)
    updated = turn.complete(messages)
    return replace(turns, index, updated), updated


def abort(turns: tuple[Turn, ...], turn_id: str) -> tuple[tuple[Turn, ...], Turn | None]:
    """Remove an active Turn and make its sequence available again."""
    validate_state(turns)
    index, turn = find(turns, turn_id)
    if turn is None:
        return turns, None
    if turn.completed:
        raise MemoryTurnConflictError("A completed Turn cannot be aborted.")
    return (*turns[:index], *turns[index + 1 :]), turn


def find(turns: tuple[Turn, ...], turn_id: str) -> tuple[int, Turn | None]:
    """Find a Turn by ID without fabricating a missing value."""
    for index, turn in enumerate(turns):
        if turn.turn_id == turn_id:
            return index, turn
    return -1, None


def replace(turns: tuple[Turn, ...], index: int, turn: Turn) -> tuple[Turn, ...]:
    """Return a tuple with one Turn replaced at its existing position."""
    if index < 0 or index >= len(turns):
        raise MemoryDataCorruptedError("The in-memory Turn replacement index is invalid.")
    return (*turns[:index], turn, *turns[index + 1 :])


def check_message_ids(turns: tuple[Turn, ...], turn_id: str, messages: tuple[Message, ...]) -> None:
    """Reject a message ID that belongs to another Turn in the same memory."""
    other_ids = {message.message_id for turn in turns if turn.turn_id != turn_id for message in turn.messages}
    if any(message.message_id in other_ids for message in messages):
        raise MemoryTurnConflictError("A message ID already belongs to another Turn.")
