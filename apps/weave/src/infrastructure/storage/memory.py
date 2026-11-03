import asyncio
import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, TextIO
from urllib.parse import quote

from pleiad.capabilities.search import BM25Index
from pleiad.contracts import ContractModel
from pleiad.errors.base import BusinessError
from pleiad.memory.short_memory import ShortMemory
from pleiad.tools.contracts import ToolDescriptor
from pydantic import AwareDatetime, Field


class MemoryFact(ContractModel):
    """Persist a host-authored product fact outside SDK conversation Turns."""

    memory_id: str = Field(min_length=1)
    scope: Literal["conversation", "user"]
    scope_id: str = Field(min_length=1)
    content: str = Field(min_length=1)
    revision: int = Field(default=1, ge=1)
    active: bool = True
    source: Literal["local_user"] | None = None
    recorded_at: AwareDatetime | None = None


class MemoryConflictError(BusinessError):
    """Reject a memory write based on an obsolete or invalid revision."""

    def __init__(self, memory_id: str, expected_revision: int, current_revision: int) -> None:
        """Describe a conflict without exposing the stored fact content."""
        super().__init__(
            "weave.memory_conflict",
            f"Memory {memory_id} expected revision {expected_revision}; current revision is {current_revision}.",
        )


class MemoryStore:
    """Bind SDK ShortMemory and explicit product recall authorization."""

    def __init__(self, root: Path, *, user_id: str, cross_conversation: bool = False) -> None:
        """Create the product memory owner without reading another user's records."""
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.user_id = user_id
        self.cross_conversation = cross_conversation
        self.memories: dict[str, ShortMemory] = {}
        self.session_locks: dict[str, asyncio.Lock] = {}

    def session(self, conversation_id: str) -> ShortMemory:
        """Reuse the SDK handle for one durable conversation stream."""
        if conversation_id not in self.memories:
            self.memories[conversation_id] = ShortMemory.sqlite(conversation_id, self.root / "short-memory.sqlite3")
        return self.memories[conversation_id]

    def session_lock(self, conversation_id: str) -> asyncio.Lock:
        """Serialize product Executions sharing one conversation."""
        return self.session_locks.setdefault(conversation_id, asyncio.Lock())

    def save_fact(self, fact: MemoryFact) -> None:
        """Create one host-authored revision-1 fact without replacing prior facts.

        Updates and deactivation must use their revision-checked management
        methods. Existing facts without provenance retain unknown provenance.
        """
        self.authorize_scope(fact.scope, fact.scope_id)
        with self.locked_journal(fact.scope, fact.scope_id, exclusive=True) as stream:
            records = self.read_revisions(stream, fact.scope, fact.scope_id)
            previous = next((record for record in reversed(records) if record.memory_id == fact.memory_id), None)
            if fact.revision != 1:
                raise ValueError("New memory facts must start at revision 1; updates require expected_revision")
            if previous is not None:
                raise MemoryConflictError(fact.memory_id, 0, previous.revision)
            self.append_revision(stream, fact)

    def list_facts(
        self,
        scope: Literal["conversation", "user"],
        *,
        conversation_id: str | None = None,
        include_inactive: bool = False,
    ) -> tuple[MemoryFact, ...]:
        """List current revisions in a locally authorized deployment scope."""
        scope_id = self.management_scope_id(scope, conversation_id)
        latest: dict[str, MemoryFact] = {}
        for fact in self.scope_history(scope, scope_id):
            latest[fact.memory_id] = fact
        return tuple(fact for fact in latest.values() if include_inactive or fact.active)

    def fact_history(
        self,
        scope: Literal["conversation", "user"],
        memory_id: str,
        *,
        conversation_id: str | None = None,
    ) -> tuple[MemoryFact, ...]:
        """Return all revisions and their original, possibly unknown provenance."""
        scope_id = self.management_scope_id(scope, conversation_id)
        return tuple(fact for fact in self.scope_history(scope, scope_id) if fact.memory_id == memory_id)

    def update_fact(
        self,
        scope: Literal["conversation", "user"],
        memory_id: str,
        content: str,
        *,
        expected_revision: int,
        conversation_id: str | None = None,
    ) -> MemoryFact:
        """Append a locally authored content revision if its base is current."""
        return self.revise_fact(scope, memory_id, expected_revision, conversation_id, content=content)

    def deactivate_fact(
        self,
        scope: Literal["conversation", "user"],
        memory_id: str,
        *,
        expected_revision: int,
        conversation_id: str | None = None,
    ) -> MemoryFact:
        """Keep a fact's history while excluding its next revision from recall."""
        return self.revise_fact(scope, memory_id, expected_revision, conversation_id, active=False)

    def revise_fact(
        self,
        scope: Literal["conversation", "user"],
        memory_id: str,
        expected_revision: int,
        conversation_id: str | None,
        *,
        content: str | None = None,
        active: bool | None = None,
    ) -> MemoryFact:
        """Validate and append one management revision under a cross-process lock."""
        if expected_revision < 1:
            raise ValueError("expected_revision must be positive")
        scope_id = self.management_scope_id(scope, conversation_id)
        with self.locked_journal(scope, scope_id, exclusive=True) as stream:
            records = self.read_revisions(stream, scope, scope_id)
            previous = next((record for record in reversed(records) if record.memory_id == memory_id), None)
            if previous is None:
                raise LookupError(f"Memory fact not found: {memory_id}")
            if previous.revision != expected_revision:
                raise MemoryConflictError(memory_id, expected_revision, previous.revision)
            updated = MemoryFact(
                memory_id=memory_id,
                scope=scope,
                scope_id=scope_id,
                content=previous.content if content is None else content,
                active=previous.active if active is None else active,
                revision=previous.revision + 1,
                source="local_user",
                recorded_at=datetime.now(UTC),
            )
            self.append_revision(stream, updated)
            return updated

    def management_scope_id(self, scope: Literal["conversation", "user"], conversation_id: str | None) -> str:
        """Resolve user identity from host settings, never from a management payload."""
        if scope == "conversation":
            if not conversation_id:
                raise ValueError("conversation_id is required for conversation memory")
            return conversation_id
        if conversation_id is not None:
            raise ValueError("conversation_id is only valid for conversation memory")
        self.authorize_scope(scope, self.user_id)
        return self.user_id

    def authorize_scope(self, scope: Literal["conversation", "user"], scope_id: str) -> None:
        """Enforce the deployment's existing cross-conversation user-memory opt-in."""
        if scope == "user" and (not self.cross_conversation or scope_id != self.user_id):
            raise PermissionError("User memory is outside the configured authorization scope")

    def scope_history(self, scope: Literal["conversation", "user"], scope_id: str) -> tuple[MemoryFact, ...]:
        """Read a complete journal while excluding concurrent partial appends."""
        self.authorize_scope(scope, scope_id)
        if not self.fact_path(scope, scope_id).exists():
            return ()
        with self.locked_journal(scope, scope_id, exclusive=False) as stream:
            return self.read_revisions(stream, scope, scope_id)

    @contextmanager
    def locked_journal(
        self, scope: Literal["conversation", "user"], scope_id: str, *, exclusive: bool
    ) -> Iterator[TextIO]:
        """Keep journal identity stable while coordinating local SDK and CLI processes."""
        path = self.fact_path(scope, scope_id)
        if exclusive:
            path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+" if exclusive else "r", encoding="utf-8") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            try:
                stream.seek(0)
                yield stream
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def read_revisions(
        self, stream: TextIO, scope: Literal["conversation", "user"], scope_id: str
    ) -> tuple[MemoryFact, ...]:
        """Reject mismatched scopes and broken per-fact revision chains."""
        records: list[MemoryFact] = []
        revisions: dict[str, int] = {}
        for line in stream:
            fact = MemoryFact.model_validate_json(line)
            if fact.scope != scope or fact.scope_id != scope_id:
                raise ValueError("Memory fact scope does not match its journal")
            if fact.revision != revisions.get(fact.memory_id, 0) + 1:
                raise ValueError(f"Memory fact has an invalid revision chain: {fact.memory_id}")
            revisions[fact.memory_id] = fact.revision
            records.append(fact)
        return tuple(records)

    def append_revision(self, stream: TextIO, fact: MemoryFact) -> None:
        """Flush one validated journal record before releasing its exclusive lock."""
        stream.seek(0, os.SEEK_END)
        stream.write(fact.model_dump_json() + "\n")
        stream.flush()
        os.fsync(stream.fileno())

    def recall(self, conversation_id: str, query: str, limit: int) -> tuple[MemoryFact, ...]:
        """Search current fact revisions only in the authorized scopes."""
        records = list(self.list_facts("conversation", conversation_id=conversation_id))
        if self.cross_conversation:
            records.extend(self.list_facts("user"))
        index = BM25Index(
            ToolDescriptor(name=fact.memory_id, code=str(position), description=fact.content)
            for position, fact in enumerate(records)
        )
        return tuple(records[int(item.code)] for item in index.search(query, limit))

    def fact_path(self, scope: Literal["conversation", "user"], scope_id: str) -> Path:
        """Encode product scope IDs without permitting path traversal."""
        return self.root / "facts" / scope / f"{quote(scope_id, safe='')}.jsonl"
