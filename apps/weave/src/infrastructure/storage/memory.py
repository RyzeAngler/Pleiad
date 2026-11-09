import fcntl
import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Literal, TextIO
from urllib.parse import quote

from pleiad.capabilities.search import BM25Index
from pleiad.contracts import ContractModel
from pleiad.errors.base import BusinessError
from pleiad.tools.contracts import ToolDescriptor
from pydantic import AwareDatetime, Field


class MemoryFact(ContractModel):
    """Persist a host-authored product fact outside SDK space ModelSteps."""

    memory_id: str = Field(min_length=1)
    scope: Literal["space", "user"]
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


class ReclaimedFactScope(ContractModel):
    """Fence a physically reclaimed Space journal against stale writes and retries."""

    space_id: str
    generation: str


class FactStore:
    """Own authorized Space and user facts independently of SDK Sessions."""

    def __init__(self, root: Path, *, user_id: str, cross_space: bool = False) -> None:
        """Create the product memory owner without reading another user's records."""
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.user_id = user_id
        self.cross_space = cross_space

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
        scope: Literal["space", "user"],
        *,
        space_id: str | None = None,
        include_inactive: bool = False,
    ) -> tuple[MemoryFact, ...]:
        """List current revisions in a locally authorized deployment scope."""
        scope_id = self.management_scope_id(scope, space_id)
        latest: dict[str, MemoryFact] = {}
        for fact in self.scope_history(scope, scope_id):
            latest[fact.memory_id] = fact
        return tuple(fact for fact in latest.values() if include_inactive or fact.active)

    def fact_history(
        self,
        scope: Literal["space", "user"],
        memory_id: str,
        *,
        space_id: str | None = None,
    ) -> tuple[MemoryFact, ...]:
        """Return all revisions and their original, possibly unknown provenance."""
        scope_id = self.management_scope_id(scope, space_id)
        return tuple(fact for fact in self.scope_history(scope, scope_id) if fact.memory_id == memory_id)

    def update_fact(
        self,
        scope: Literal["space", "user"],
        memory_id: str,
        content: str,
        *,
        expected_revision: int,
        space_id: str | None = None,
    ) -> MemoryFact:
        """Append a locally authored content revision if its base is current."""
        return self.revise_fact(scope, memory_id, expected_revision, space_id, content=content)

    def deactivate_fact(
        self,
        scope: Literal["space", "user"],
        memory_id: str,
        *,
        expected_revision: int,
        space_id: str | None = None,
    ) -> MemoryFact:
        """Keep a fact's history while excluding its next revision from recall."""
        return self.revise_fact(scope, memory_id, expected_revision, space_id, active=False)

    def revise_fact(
        self,
        scope: Literal["space", "user"],
        memory_id: str,
        expected_revision: int,
        space_id: str | None,
        *,
        content: str | None = None,
        active: bool | None = None,
    ) -> MemoryFact:
        """Validate and append one management revision under a cross-process lock."""
        if expected_revision < 1:
            raise ValueError("expected_revision must be positive")
        scope_id = self.management_scope_id(scope, space_id)
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

    def management_scope_id(self, scope: Literal["space", "user"], space_id: str | None) -> str:
        """Resolve user identity from host settings, never from a management payload."""
        if scope == "space":
            if not space_id:
                raise ValueError("space_id is required for space memory")
            return space_id
        if space_id is not None:
            raise ValueError("space_id is only valid for space memory")
        self.authorize_scope(scope, self.user_id)
        return self.user_id

    def authorize_scope(self, scope: Literal["space", "user"], scope_id: str) -> None:
        """Enforce the deployment's existing cross-space user-memory opt-in."""
        if scope == "user" and (not self.cross_space or scope_id != self.user_id):
            raise PermissionError("User memory is outside the configured authorization scope")

    def scope_history(self, scope: Literal["space", "user"], scope_id: str) -> tuple[MemoryFact, ...]:
        """Read a complete journal while excluding partial appends and scope reclamation."""
        self.authorize_scope(scope, scope_id)
        with self.scope_lock(scope, scope_id, exclusive=False):
            path = self.fact_path(scope, scope_id)
            if not path.exists() or (scope == "space" and self.reclaimed_scope_path(scope_id).exists()):
                return ()
            with path.open(encoding="utf-8") as stream:
                return self.read_revisions(stream, scope, scope_id)

    @contextmanager
    def scope_lock(self, scope: Literal["space", "user"], scope_id: str, *, exclusive: bool) -> Iterator[None]:
        """Keep one permanent lock identity across journal replacement and deletion."""
        lock = self.root / "fact-locks" / scope / f"{quote(scope_id, safe='')}.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        with lock.open("a+b") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    @contextmanager
    def locked_journal(self, scope: Literal["space", "user"], scope_id: str, *, exclusive: bool) -> Iterator[TextIO]:
        """Coordinate management writes with generation-fenced Space cleanup."""
        path = self.fact_path(scope, scope_id)
        with self.scope_lock(scope, scope_id, exclusive=exclusive):
            if scope == "space" and self.reclaimed_scope_path(scope_id).exists():
                raise PermissionError("The Space memory scope has been reclaimed")
            if exclusive:
                path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a+" if exclusive else "r", encoding="utf-8") as stream:
                stream.seek(0)
                yield stream

    def reclaimed_scope_path(self, space_id: str) -> Path:
        """Return the host-owned tombstone path for one Space fact scope."""
        return self.root / "reclaimed-fact-scopes" / f"{quote(space_id, safe='')}.json"

    def delete_space(self, space_id: str, *, generation: str) -> None:
        """Reclaim only a Space's facts and reject stale owners or subsequent writes."""
        if not space_id.strip() or re.fullmatch(r"[A-Za-z0-9_-]+", generation) is None:
            raise ValueError("Fact cleanup requires a Space identity and generation")
        with self.scope_lock("space", space_id, exclusive=True):
            marker = self.reclaimed_scope_path(space_id)
            if marker.exists():
                recorded = ReclaimedFactScope.model_validate_json(marker.read_text(encoding="utf-8"))
                if recorded.space_id != space_id or recorded.generation != generation:
                    raise ValueError("The fact cleanup generation is obsolete")
            else:
                marker.parent.mkdir(parents=True, exist_ok=True)
                with NamedTemporaryFile(mode="w", encoding="utf-8", dir=marker.parent, delete=False) as stream:
                    stream.write(ReclaimedFactScope(space_id=space_id, generation=generation).model_dump_json())
                    stream.flush()
                    os.fsync(stream.fileno())
                    temporary = Path(stream.name)
                os.replace(temporary, marker)
            source = self.fact_path("space", space_id)
            destination = self.root / "recycle" / generation / "facts" / source.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            if source.is_symlink():
                raise ValueError("Fact cleanup refuses a symbolic-link journal")
            if source.exists():
                if destination.exists():
                    raise ValueError("The fact journal was recreated during cleanup")
                os.replace(source, destination)
            destination.unlink(missing_ok=True)

    def read_revisions(self, stream: TextIO, scope: Literal["space", "user"], scope_id: str) -> tuple[MemoryFact, ...]:
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

    def recall(self, space_id: str, query: str, limit: int) -> tuple[MemoryFact, ...]:
        """Search current fact revisions only in the authorized scopes."""
        records = list(self.list_facts("space", space_id=space_id))
        if self.cross_space:
            records.extend(self.list_facts("user"))
        index = BM25Index(
            ToolDescriptor(name=fact.memory_id, code=str(position), description=fact.content)
            for position, fact in enumerate(records)
        )
        return tuple(records[int(item.code)] for item in index.search(query, limit))

    def fact_path(self, scope: Literal["space", "user"], scope_id: str) -> Path:
        """Encode product scope IDs without permitting path traversal."""
        return self.root / "facts" / scope / f"{quote(scope_id, safe='')}.jsonl"
