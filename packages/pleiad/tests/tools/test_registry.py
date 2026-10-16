from collections.abc import Callable

from pleiad.contracts import ContractModel, JsonObject
from pleiad.tools.base import FunctionTool
from pleiad.tools.contracts import ToolContext, ToolDescriptor
from pleiad.tools.registry import ToolRegistry


class EmptyArguments(ContractModel):
    """Parameter contract for a Tool without required arguments."""

    pass


def make_factory() -> Callable[[ToolDescriptor], FunctionTool]:
    async def execute(context: ToolContext, arguments: JsonObject) -> JsonObject:
        return {"value": "ok"}

    def create(descriptor: ToolDescriptor) -> FunctionTool:
        return FunctionTool(descriptor, execute, EmptyArguments)

    return create


def test_registry_loads_a_fresh_tool_instance_for_each_resolution() -> None:
    descriptor = ToolDescriptor(name="lookup", code="records.lookup", description="Look up records")
    registry = ToolRegistry()
    registry.register(descriptor, make_factory())

    first = registry.load(descriptor.code)
    second = registry.load(descriptor.code)

    assert first is not second
    assert first.descriptor == second.descriptor == descriptor


def test_registry_attaches_manifest_metadata_to_a_registered_factory() -> None:
    descriptor = ToolDescriptor(name="lookup", code="records.lookup", description="Look up records")
    registry = ToolRegistry()
    registry.register_factory(descriptor.code, make_factory())

    registry.register_descriptor(descriptor)

    assert registry.resolve(descriptor.code) == descriptor
    assert registry.codes() == (descriptor.code,)
