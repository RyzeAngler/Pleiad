"""The single executable capability interface."""

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any, ParamSpec, TypeVar, cast

from pleiad.contracts import JsonObject, JsonSchema
from pleiad.llm.contracts import ToolDefinition
from pleiad.tools.contracts import ToolContext, ToolDescriptor, ToolPolicy


class BaseTool(ABC):
    """A loaded, executable capability exposed to an Agent."""

    @property
    @abstractmethod
    def descriptor(self) -> ToolDescriptor:
        """Return the stable descriptor selected by the Zoo."""

    @property
    @abstractmethod
    def input_schema(self) -> JsonSchema:
        """Return the JSON Schema presented to the model."""

    @property
    def output_schema(self) -> JsonSchema:
        """Return the JSON Schema used to validate the Tool result."""
        return {"type": "object"}

    @property
    def policy(self) -> ToolPolicy:
        """Return runtime safety defaults for this Tool."""
        return ToolPolicy()

    @property
    def model_definition(self) -> ToolDefinition:
        """Project a Tool into the provider-neutral LLM contract."""
        return ToolDefinition(
            name=self.descriptor.name,
            description=self.descriptor.description,
            input_schema=self.input_schema,
        )

    @abstractmethod
    async def execute(self, context: ToolContext, arguments: JsonObject) -> JsonObject:
        """Execute validated arguments in the supplied runtime context."""


class FunctionTool(BaseTool):
    """Turn one async function into a first-class BaseTool."""

    def __init__(
        self,
        descriptor: ToolDescriptor,
        function: Callable[[ToolContext, JsonObject], Awaitable[JsonObject]],
        input_schema: JsonSchema,
        output_schema: JsonSchema | None = None,
        policy: ToolPolicy | None = None,
    ) -> None:
        self._descriptor = descriptor
        self._function = function
        self._input_schema = input_schema
        self._output_schema = output_schema or {"type": "object"}
        self._policy = policy or ToolPolicy()

    @property
    def descriptor(self) -> ToolDescriptor:
        return self._descriptor

    @property
    def input_schema(self) -> JsonSchema:
        return self._input_schema

    @property
    def output_schema(self) -> JsonSchema:
        return self._output_schema

    @property
    def policy(self) -> ToolPolicy:
        return self._policy

    async def execute(self, context: ToolContext, arguments: JsonObject) -> JsonObject:
        return await self._function(context, arguments)


P = ParamSpec("P")
R = TypeVar("R")


def tool(
    *,
    name: str,
    code: str,
    description: str,
    input_schema: JsonSchema,
    output_schema: JsonSchema | None = None,
    policy: ToolPolicy | None = None,
) -> Callable[[Callable[[ToolContext, JsonObject], Awaitable[JsonObject]]], Callable[..., Any]]:
    """Decorate a function with the metadata needed by ToolLoader.

    The decorator only stores registration metadata. It does not expose a new
    user-facing configuration language or turn a Python target into YAML.
    """
    descriptor = ToolDescriptor(name=name, code=code, description=description)

    def decorate(
        function: Callable[[ToolContext, JsonObject], Awaitable[JsonObject]],
    ) -> Callable[..., Any]:
        """Attach a private registration record to the original function."""
        cast(Any, function).__pleiad_tool_registration__ = {
            "descriptor": descriptor,
            "input_schema": input_schema,
            "output_schema": output_schema,
            "policy": policy,
        }
        return wraps(function)(function)

    return decorate
