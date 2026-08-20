"""Searchable Tool descriptor catalog and immutable selection snapshots."""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from pleiad.capabilities import BM25Index
from pleiad.tools.base import BaseTool
from pleiad.tools.contracts import ToolDescriptor
from pleiad.tools.loader import ToolFactory, ToolLoader


@dataclass(frozen=True, slots=True)
class ToolZooSnapshot:
    """A point-in-time set of descriptors selected for one Agent."""

    snapshot_id: str
    created_at: datetime
    descriptors: tuple[ToolDescriptor, ...]

    def select(self, codes: tuple[str, ...] | list[str]) -> tuple[ToolDescriptor, ...]:
        """Select descriptors by code while preserving the requested order."""
        index = {descriptor.code: descriptor for descriptor in self.descriptors}
        selected: list[ToolDescriptor] = []
        for code in codes:
            if code not in index:
                raise LookupError(f"Tool code is not in this snapshot: {code}")
            selected.append(index[code])
        return tuple(selected)


class ToolZoo:
    """Descriptor index plus lazy implementation loading."""

    def __init__(self, loader: ToolLoader | None = None) -> None:
        self.loader = loader or ToolLoader()
        self._descriptors: dict[str, ToolDescriptor] = {}
        self._builtin_codes: set[str] = set()
        self._search_index: BM25Index[ToolDescriptor] = BM25Index(())

    def register(self, descriptor: ToolDescriptor, factory: ToolFactory) -> None:
        """Register descriptor metadata and its lazy implementation."""
        if descriptor.code in self._descriptors:
            raise ValueError(f"Tool code is already registered: {descriptor.code}")
        self._descriptors[descriptor.code] = descriptor
        try:
            self.loader.register(descriptor.code, factory)
        except Exception:
            del self._descriptors[descriptor.code]
            raise
        self._rebuild_search_index()

    def register_builtin(self, descriptor: ToolDescriptor, factory: ToolFactory) -> None:
        """Register one process-provided Tool that every Agent receives by default."""
        self.register(descriptor, factory)
        self._builtin_codes.add(descriptor.code)

    def register_factory(self, code: str, factory: ToolFactory) -> None:
        """Register an implementation code before YAML publishes its descriptor."""
        self.loader.register(code, factory)

    def register_loaded(self, loaded: BaseTool) -> None:
        """Register an already constructed Tool for process-local integrations."""
        self.register(loaded.descriptor, lambda _descriptor: loaded)

    def register_descriptor(self, descriptor: ToolDescriptor) -> None:
        """Index YAML metadata for an already registered implementation code."""
        if descriptor.code in self._descriptors:
            if self._descriptors[descriptor.code] != descriptor:
                raise ValueError(f"Tool metadata conflicts with registered code: {descriptor.code}")
            return
        if not self.loader.contains(descriptor.code):
            raise LookupError(f"No Tool implementation is registered for code: {descriptor.code}")
        self._descriptors[descriptor.code] = descriptor
        self._rebuild_search_index()

    def replace_from(self, other: "ToolZoo") -> None:
        """Replace the catalog with a validated candidate catalog."""
        self.loader = other.loader
        self._descriptors = dict(other._descriptors)
        self._builtin_codes = set(other._builtin_codes)
        self._rebuild_search_index()

    def resolve(self, code: str) -> ToolDescriptor:
        """Return one descriptor by opaque code."""
        try:
            return self._descriptors[code]
        except KeyError as exc:
            raise LookupError(f"Unknown Tool code: {code}") from exc

    def search(self, query: str = "", limit: int = 20) -> tuple[ToolDescriptor, ...]:
        """Search descriptor metadata without loading Tool implementations."""
        return self._search_index.search(query, limit)

    def _rebuild_search_index(self) -> None:
        """Refresh the derived search view after a catalog mutation."""
        self._search_index = BM25Index(self._descriptors.values())

    def load(self, code: str) -> BaseTool:
        """Lazily load one selected Tool."""
        return self.loader.load(self.resolve(code))

    def builtin_codes(self) -> tuple[str, ...]:
        """Return the stable codes of Tools loaded into every Agent."""
        return tuple(sorted(self._builtin_codes))

    def builtin_tools(self) -> tuple[BaseTool, ...]:
        """Load the complete default Tool set for one Agent definition."""
        return tuple(self.load(code) for code in self.builtin_codes())

    def snapshot(self) -> ToolZooSnapshot:
        """Capture current metadata for an Agent definition."""
        return ToolZooSnapshot(
            snapshot_id=uuid4().hex,
            created_at=datetime.now(UTC),
            descriptors=tuple(sorted(self._descriptors.values(), key=lambda item: item.code)),
        )

    def codes(self) -> tuple[str, ...]:
        """Return all known descriptor codes."""
        return tuple(sorted(self._descriptors))
