from collections.abc import Callable

from pleiad.capabilities.search import BM25Index
from pleiad.tools.base import BaseTool
from pleiad.tools.contracts import ToolDescriptor

type ToolFactory = Callable[[ToolDescriptor], BaseTool]


class ToolRegistry:
    """Own Tool descriptors and their lazy executable bindings."""

    def __init__(self) -> None:
        self.descriptors: dict[str, ToolDescriptor] = {}
        self.factories: dict[str, ToolFactory] = {}
        self.search_index: BM25Index[ToolDescriptor] = BM25Index(())

    def register(self, descriptor: ToolDescriptor, factory: ToolFactory) -> None:
        """Register one complete descriptor-to-factory binding."""
        if descriptor.code in self.descriptors or descriptor.code in self.factories:
            raise ValueError(f"Tool code is already registered: {descriptor.code}")
        self.descriptors[descriptor.code] = descriptor
        self.factories[descriptor.code] = factory
        self.rebuild_search_index()

    def register_factory(self, code: str, factory: ToolFactory) -> None:
        """Register an implementation before its manifest descriptor is loaded."""
        if not code.strip():
            raise ValueError("Tool factory code must be non-empty")
        if code in self.factories or code in self.descriptors:
            raise ValueError(f"Tool code is already registered: {code}")
        self.factories[code] = factory

    def register_descriptor(self, descriptor: ToolDescriptor) -> None:
        """Attach manifest metadata to an already registered implementation."""
        if descriptor.code in self.descriptors:
            raise ValueError(f"Tool code is already registered: {descriptor.code}")
        if descriptor.code not in self.factories:
            raise LookupError(f"No Tool implementation is registered for code: {descriptor.code}")
        self.descriptors[descriptor.code] = descriptor
        self.rebuild_search_index()

    def contains(self, code: str) -> bool:
        """Return whether an implementation binding is registered for a code."""
        return code in self.factories

    def resolve(self, code: str) -> ToolDescriptor:
        """Return the manifest descriptor for one registered Tool."""
        try:
            return self.descriptors[code]
        except KeyError as exc:
            raise LookupError(f"Unknown Tool code: {code}") from exc

    def search(self, query: str = "", limit: int = 20) -> tuple[ToolDescriptor, ...]:
        """Search registered descriptors without constructing Tool instances."""
        return self.search_index.search(query, limit)

    def load(self, code: str) -> BaseTool:
        """Construct a fresh Tool instance for one resolved descriptor."""
        descriptor = self.resolve(code)
        try:
            factory = self.factories[code]
        except KeyError as exc:
            raise LookupError(f"No Tool implementation is registered for code: {code}") from exc
        tool = factory(descriptor)
        if tool.descriptor.code != code:
            raise ValueError(f"Tool factory returned code {tool.descriptor.code!r} for {code!r}")
        return tool

    def codes(self) -> tuple[str, ...]:
        """Return descriptor codes in deterministic order."""
        return tuple(sorted(self.descriptors))

    def rebuild_search_index(self) -> None:
        """Refresh the derived search index after descriptor registration."""
        self.search_index = BM25Index(self.descriptors.values())
