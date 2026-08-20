"""Load executable Tools from opaque, stable codes."""

from collections.abc import Callable

from pleiad.tools.base import BaseTool
from pleiad.tools.contracts import ToolDescriptor

ToolFactory = Callable[[ToolDescriptor], BaseTool]


class ToolLoader:
    """Resolve a Tool descriptor into an executable BaseTool."""

    def __init__(self) -> None:
        self._factories: dict[str, ToolFactory] = {}

    def register(self, code: str, factory: ToolFactory) -> None:
        """Register one stable code exactly once."""
        if code in self._factories:
            raise ValueError(f"Tool code is already registered: {code}")
        self._factories[code] = factory

    def load(self, descriptor: ToolDescriptor) -> BaseTool:
        """Load the implementation selected by a descriptor's code."""
        try:
            factory = self._factories[descriptor.code]
        except KeyError as exc:
            raise LookupError(f"No Tool loader registered for code: {descriptor.code}") from exc
        tool = factory(descriptor)
        if tool.descriptor.code != descriptor.code:
            raise ValueError(
                f"Tool loader returned code {tool.descriptor.code!r} for {descriptor.code!r}"
            )
        return tool

    def contains(self, code: str) -> bool:
        """Return whether a resolver is registered for a code."""
        return code in self._factories

    def codes(self) -> tuple[str, ...]:
        """Return registered codes in deterministic order."""
        return tuple(sorted(self._factories))
