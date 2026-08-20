"""Searchable Skill descriptor catalog and selection snapshots."""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from pleiad.capabilities import BM25Index
from pleiad.skills.contracts import Skill, SkillDescriptor
from pleiad.skills.loader import SkillFactory, SkillLoader


@dataclass(frozen=True, slots=True)
class SkillZooSnapshot:
    """A point-in-time set of Skill descriptors."""

    snapshot_id: str
    created_at: datetime
    descriptors: tuple[SkillDescriptor, ...]

    def select(self, codes: tuple[str, ...] | list[str]) -> tuple[SkillDescriptor, ...]:
        """Select descriptors by code while preserving order."""
        index = {descriptor.code: descriptor for descriptor in self.descriptors}
        try:
            return tuple(index[code] for code in codes)
        except KeyError as exc:
            raise LookupError(f"Skill code is not in this snapshot: {exc.args[0]}") from exc


class SkillZoo:
    """Skill metadata index with lazy static content loading."""

    def __init__(self, loader: SkillLoader | None = None) -> None:
        self.loader = loader or SkillLoader()
        self._descriptors: dict[str, SkillDescriptor] = {}
        self._search_index: BM25Index[SkillDescriptor] = BM25Index(())

    def register(self, descriptor: SkillDescriptor, factory: SkillFactory) -> None:
        """Register descriptor metadata and its loader."""
        if descriptor.code in self._descriptors:
            raise ValueError(f"Skill code is already registered: {descriptor.code}")
        self._descriptors[descriptor.code] = descriptor
        try:
            self.loader.register(descriptor.code, factory)
        except Exception:
            del self._descriptors[descriptor.code]
            raise
        self._rebuild_search_index()

    def resolve(self, code: str) -> SkillDescriptor:
        """Return one descriptor by code."""
        try:
            return self._descriptors[code]
        except KeyError as exc:
            raise LookupError(f"Unknown Skill code: {code}") from exc

    def register_descriptor(self, descriptor: SkillDescriptor) -> None:
        """Index YAML metadata for an already registered Skill code."""
        if descriptor.code in self._descriptors:
            raise ValueError(f"Skill code is already registered: {descriptor.code}")
        if not self.loader.contains(descriptor.code):
            raise LookupError(f"No Skill implementation is registered for code: {descriptor.code}")
        self._descriptors[descriptor.code] = descriptor
        self._rebuild_search_index()

    def replace_from(self, other: "SkillZoo") -> None:
        """Replace the catalog with a validated candidate catalog."""
        self.loader = other.loader
        self._descriptors = dict(other._descriptors)
        self._rebuild_search_index()

    def search(self, query: str = "", limit: int = 20) -> tuple[SkillDescriptor, ...]:
        """Search descriptor metadata without loading Skill bodies."""
        return self._search_index.search(query, limit)

    def _rebuild_search_index(self) -> None:
        """Refresh the derived search view after a catalog mutation."""
        self._search_index = BM25Index(self._descriptors.values())

    def load(self, code: str) -> Skill:
        """Lazily load one selected Skill."""
        return self.loader.load(self.resolve(code))

    def snapshot(self) -> SkillZooSnapshot:
        """Capture current metadata for one Agent definition."""
        return SkillZooSnapshot(
            snapshot_id=uuid4().hex,
            created_at=datetime.now(UTC),
            descriptors=tuple(sorted(self._descriptors.values(), key=lambda item: item.code)),
        )
