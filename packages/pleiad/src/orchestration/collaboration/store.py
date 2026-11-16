from abc import ABC, abstractmethod

from pleiad.orchestration.collaboration.errors import CollaborationStorageError
from pleiad.orchestration.collaboration.state import CollaborationState


def validate_write(current: CollaborationState | None, proposed: CollaborationState) -> CollaborationState:
    """Check revision and owner fencing before committing a detached journal."""
    if current is None:
        if proposed.revision != 1 or proposed.owner_epoch != 1:
            raise CollaborationStorageError("New scopes require revision and owner epoch one.")
    elif proposed.revision != current.revision + 1 or proposed.owner_epoch != current.owner_epoch:
        raise CollaborationStorageError("The collaboration writer is stale.")
    return CollaborationState.model_validate_json(proposed.model_dump_json())


class CollaborationStore(ABC):
    """Own atomic scope journals independently from Session persistence."""

    @abstractmethod
    async def read(self, scope_id: str) -> CollaborationState | None:
        """Read a detached committed snapshot without acquiring ownership."""

    @abstractmethod
    async def write(self, state: CollaborationState) -> None:
        """Commit one fenced revision, including commands, events, and outbox."""

    @abstractmethod
    async def claim(self, scope_id: str, expected_epoch: int) -> CollaborationState:
        """Fence a dead owner after the host establishes exclusive deployment ownership."""
