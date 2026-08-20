"""Incremental JSONL persistence for main-Agent Memory."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from threading import RLock
from typing import TypeVar
from uuid import uuid4

from pleiad.context.contracts import ContextCheckpoint
from pleiad.contracts import ContractModel
from pleiad.llm.contracts import Message, MessageRole
from pleiad.memory.model import (
    ConversationMessage,
    ConversationRole,
    LongTermMemory,
    MainAgentMessage,
    MemoryCandidate,
    MemoryPolicy,
    MemoryScope,
    MemoryStatus,
)
from pleiad.memory.search import LexicalMemorySearch
from pleiad.memory.store import MainAgentTurn

RecordT = TypeVar("RecordT", bound=ContractModel)


class JsonlMainAgentMemory:
    """Persist main-Agent Memory as append-only JSONL journals."""

    def __init__(
        self,
        root: Path,
        policy: MemoryPolicy | None = None,
    ) -> None:
        self.root = root
        self.policy = policy or MemoryPolicy()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._sequences: dict[str, int] = {}
        self._search = LexicalMemorySearch()

    def begin_turn(
        self,
        conversation_id: str,
        execution_id: str,
        raw_query: str,
    ) -> MainAgentTurn:
        """Persist the raw query and restore short-term context once."""
        with self._lock:
            session = self._read_session(conversation_id)
            history_sequence = max(
                (record.sequence for record in session),
                default=0,
            )
            checkpoint = self._latest_checkpoint_locked(conversation_id)
            covered_until = checkpoint.covered_until_sequence if checkpoint else 0
            history = tuple(record.message for record in session if record.sequence > covered_until)
            user_message = ConversationMessage(
                message_id=uuid4().hex,
                conversation_id=conversation_id,
                execution_id=execution_id,
                role=ConversationRole.USER,
                content=raw_query,
            )
            self._append(self._dialogue_path(conversation_id), user_message)
            self._append_session(
                conversation_id,
                MainAgentMessage(
                    message_id=uuid4().hex,
                    conversation_id=conversation_id,
                    execution_id=execution_id,
                    turn_id=execution_id,
                    run_id=None,
                    sequence=self._next_sequence(conversation_id),
                    message=Message(role=MessageRole.USER, content=raw_query),
                    source_conversation_message_id=user_message.message_id,
                ),
            )
            personal_profile = self._profile_locked()

        return MainAgentTurn(
            conversation_id=conversation_id,
            execution_id=execution_id,
            user_message=user_message,
            history=history,
            personal_profile=personal_profile,
            append_model_message=lambda run_id, message: self.append_model_message(
                conversation_id,
                execution_id,
                execution_id,
                run_id,
                message,
            ),
            history_sequence=history_sequence,
            checkpoint_handoff=checkpoint.handoff if checkpoint else None,
            append_checkpoint=self.append_checkpoint,
        )

    def recall_memory(
        self,
        *,
        conversation_id: str,
        query: str,
        limit: int,
        policy: MemoryPolicy,
    ) -> tuple[MemoryCandidate, ...]:
        """Search L1 and, when enabled, permitted L2 records."""
        with self._lock:
            records = list(self._active_records(self._summary_path(conversation_id)))
            if self.policy.cross_conversation_memory and policy.cross_conversation_memory:
                records.extend(self._active_records(self._profile_path(policy.user_id)))
            return self._search.search(records, query, limit=min(limit, policy.recall_max_results))

    def append_model_message(
        self,
        conversation_id: str,
        execution_id: str,
        turn_id: str,
        run_id: str,
        message: Message,
    ) -> None:
        """Append one generated Assistant or Tool message."""
        if message.role is MessageRole.SYSTEM:
            raise ValueError("system prompts are request-local and cannot enter memory")
        persisted = _persistent_message(message)
        with self._lock:
            self._append_session(
                conversation_id,
                MainAgentMessage(
                    message_id=uuid4().hex,
                    conversation_id=conversation_id,
                    execution_id=execution_id,
                    turn_id=turn_id,
                    run_id=run_id,
                    sequence=self._next_sequence(conversation_id),
                    message=persisted,
                ),
            )

    def finish_turn(
        self,
        turn: MainAgentTurn,
        assistant_content: str,
        *,
        succeeded: bool,
    ) -> ConversationMessage | None:
        """Append only the final visible Assistant response to L0."""
        if not succeeded or not assistant_content.strip():
            return None
        with self._lock:
            assistant = ConversationMessage(
                message_id=uuid4().hex,
                conversation_id=turn.conversation_id,
                execution_id=turn.execution_id,
                role=ConversationRole.ASSISTANT,
                content=assistant_content,
            )
            self._append(self._dialogue_path(turn.conversation_id), assistant)
            return assistant

    def append_long_term(self, memory: LongTermMemory) -> None:
        """Append one summary or fact revision to its scope journal."""
        if memory.scope is MemoryScope.USER:
            if not self.policy.cross_conversation_memory:
                raise ValueError("user Memory is disabled by the current Memory policy")
            if memory.scope_id != self.policy.user_id:
                raise ValueError("user Memory must use the configured user_id")
        with self._lock:
            self._append(self._long_term_path(memory), memory)

    def append_checkpoint(self, checkpoint: object) -> None:
        """Persist one request-context handoff in the conversation journal."""
        if not isinstance(checkpoint, ContextCheckpoint):
            raise TypeError("checkpoint must be a ContextCheckpoint")
        with self._lock:
            self._append(self._checkpoint_path(checkpoint.conversation_id), checkpoint)

    def list_conversation(self, conversation_id: str) -> tuple[ConversationMessage, ...]:
        """Read raw external Conversation messages in append order."""
        with self._lock:
            return self._read(self._dialogue_path(conversation_id), ConversationMessage)

    def list_model_messages(self, conversation_id: str) -> tuple[MainAgentMessage, ...]:
        """Read all durable main-Agent messages in sequence order."""
        with self._lock:
            return self._read_session(conversation_id)

    def list_long_term(self, conversation_id: str) -> tuple[LongTermMemory, ...]:
        """Read active L1 revisions for one Conversation."""
        with self._lock:
            return self._active_records(self._summary_path(conversation_id))

    def list_profile(self) -> tuple[LongTermMemory, ...]:
        """Read active L2 profile revisions for the configured user."""
        with self._lock:
            if not self.policy.cross_conversation_memory:
                return ()
            return self._profile_locked_records()

    def list_checkpoints(self, conversation_id: str) -> tuple[ContextCheckpoint, ...]:
        """Read request-context checkpoints in append order."""
        with self._lock:
            return self._read(
                self._checkpoint_path(conversation_id),
                ContextCheckpoint,
            )

    def _profile_locked(self) -> tuple[MemoryCandidate, ...]:
        """Load a bounded L2 profile snapshot before one Agent Loop."""
        if not self.policy.cross_conversation_memory:
            return ()
        candidates: list[MemoryCandidate] = []
        used_chars = 0
        for record in self._profile_locked_records():
            if len(candidates) >= self.policy.profile_max_items:
                break
            if used_chars + len(record.content) > self.policy.profile_max_tokens * 4:
                break
            candidates.append(_candidate(record, None))
            used_chars += len(record.content)
        return tuple(candidates)

    def _profile_locked_records(self) -> tuple[LongTermMemory, ...]:
        """Return current profile records in newest-first order."""
        return tuple(
            sorted(
                self._active_records(self._profile_path(self.policy.user_id)),
                key=lambda record: record.updated_at,
                reverse=True,
            )
        )

    def _active_records(self, path: Path) -> tuple[LongTermMemory, ...]:
        """Read latest active revisions from one long-term journal."""
        records = self._read(path, LongTermMemory)
        latest: dict[str, LongTermMemory] = {}
        for record in records:
            previous = latest.get(record.memory_id)
            if previous is None or record.revision >= previous.revision:
                latest[record.memory_id] = record
        return tuple(
            record
            for record in sorted(latest.values(), key=lambda item: item.updated_at)
            if record.status is MemoryStatus.ACTIVE
        )

    def _append_session(self, conversation_id: str, record: MainAgentMessage) -> None:
        self._append(self._session_path(conversation_id), record)

    def _next_sequence(self, conversation_id: str) -> int:
        current = self._sequences.get(conversation_id)
        if current is None:
            current = max(
                (record.sequence for record in self._read_session(conversation_id)),
                default=0,
            )
        current += 1
        self._sequences[conversation_id] = current
        return current

    @staticmethod
    def _append(path: Path, record: ContractModel) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(record.model_dump_json())
            stream.write("\n")
            stream.flush()

    @staticmethod
    def _read(path: Path, record_type: type[RecordT]) -> tuple[RecordT, ...]:
        if not path.exists():
            return ()
        records: list[RecordT] = []
        with path.open(encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, start=1):
                if not line.strip():
                    continue
                try:
                    records.append(record_type.model_validate_json(line))
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"invalid memory record at {path}:{line_number}") from exc
        return tuple(records)

    def _read_session(self, conversation_id: str) -> tuple[MainAgentMessage, ...]:
        records = self._read(self._session_path(conversation_id), MainAgentMessage)
        return tuple(sorted(records, key=lambda record: record.sequence))

    def _conversation_root(self, conversation_id: str) -> Path:
        return self.root / "conversations" / _safe_filename(conversation_id)

    def _user_root(self, user_id: str) -> Path:
        return self.root / "users" / _safe_filename(user_id)

    def _dialogue_path(self, conversation_id: str) -> Path:
        return self._conversation_root(conversation_id) / "dialogue.jsonl"

    def _session_path(self, conversation_id: str) -> Path:
        return self._conversation_root(conversation_id) / "main-agent-session.jsonl"

    def _summary_path(self, conversation_id: str) -> Path:
        return self._conversation_root(conversation_id) / "summaries.jsonl"

    def _profile_path(self, user_id: str) -> Path:
        return self._user_root(user_id) / "profile.jsonl"

    def _checkpoint_path(self, conversation_id: str) -> Path:
        return self._conversation_root(conversation_id) / "context-checkpoints.jsonl"

    def _long_term_path(self, memory: LongTermMemory) -> Path:
        if memory.scope is MemoryScope.CONVERSATION:
            return self._summary_path(memory.scope_id)
        return self._profile_path(memory.scope_id)

    def _latest_checkpoint_locked(self, conversation_id: str) -> ContextCheckpoint | None:
        checkpoints = self._read(
            self._checkpoint_path(conversation_id),
            ContextCheckpoint,
        )
        return checkpoints[-1] if checkpoints else None


def _persistent_message(message: Message) -> Message:
    """Keep semantic model content while excluding hidden reasoning and provider noise."""
    return message.model_copy(update={"thinking": None, "provider_data": {}})


def _candidate(record: LongTermMemory, score: float | None) -> MemoryCandidate:
    """Project a durable record into a request-local profile candidate."""
    return MemoryCandidate(
        memory_id=record.memory_id,
        scope=record.scope,
        content=record.content,
        kind=record.kind,
        source_message_ids=record.source_message_ids,
        score=score,
        updated_at=record.updated_at,
        metadata=dict(record.metadata),
    )


def _safe_filename(value: str) -> str:
    """Create a readable and collision-resistant directory name."""
    readable = re.sub(r"[^A-Za-z0-9._-]", "_", value).strip("._") or "memory"
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    return f"{readable[:80]}-{digest}"
