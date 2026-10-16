from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from pleiad.contracts import ContractModel, JsonObject, JsonSchema
from pleiad.models.contracts import Tool
from pleiad.schemas import validate_json_schema
from pleiad.tools.contracts import ToolContext, ToolDescriptor, ToolPolicy
from pleiad.tools.errors import ToolError
from pleiad.tools.interruption import InterruptionResponse, ToolExecutionOutcome, ToolInterruption


@dataclass(frozen=True, slots=True)
class ToolFailure:
    """Return an expected Tool failure together with its model-visible observation."""

    error: ToolError
    output: JsonObject


type ToolOutput = JsonObject | ToolFailure | ToolInterruption


class BaseTool(ABC):
    """A loaded, executable capability exposed to an Agent."""

    @property
    @abstractmethod
    def descriptor(self) -> ToolDescriptor:
        """Return the stable descriptor selected by the ToolRegistry."""

    @property
    @abstractmethod
    def input_schema(self) -> JsonSchema:
        """Return the JSON Schema presented to the model."""

    @property
    def model_tool(self) -> Tool:
        """Project this Tool into the provider-neutral model contract."""
        return Tool(
            name=self.descriptor.name,
            description=self.descriptor.description,
            input_schema=self.input_schema,
        )

    @property
    def policy(self) -> ToolPolicy:
        """Return runtime safety defaults for this Tool."""
        return ToolPolicy()

    @property
    def required_permissions(self) -> frozenset[str]:
        """Return the capability grants required before execution."""
        return frozenset()

    @abstractmethod
    async def execute(self, context: ToolContext, arguments: JsonObject) -> ToolOutput:
        """Execute validated arguments in the supplied runtime context."""

    async def resume_interruption(
        self,
        context: ToolContext,
        interruption: ToolInterruption,
        response: InterruptionResponse,
    ) -> ToolExecutionOutcome:
        """Complete a pending Tool action using a host-provided resolution."""
        return response.result


class FunctionTool(BaseTool):
    """Turn one async function into a first-class BaseTool."""

    def __init__(
        self,
        descriptor: ToolDescriptor,
        function: Callable[[ToolContext, JsonObject], Awaitable[ToolOutput]],
        parameters_type: type[ContractModel],
        policy: ToolPolicy | None = None,
        required_permissions: frozenset[str] = frozenset(),
    ) -> None:
        """Bind a function, validated parameter model, and host execution policy."""
        self.tool_descriptor = descriptor
        self.function = function
        self.parameters_type = parameters_type
        self.tool_policy = ToolPolicy() if policy is None else policy
        if any(not permission.strip() for permission in required_permissions):
            raise ValueError("Tool permissions must be non-empty strings")
        self.permissions = frozenset(required_permissions)
        validate_json_schema(self.input_schema)

    @property
    def descriptor(self) -> ToolDescriptor:
        """Return metadata for this function binding."""
        return self.tool_descriptor

    @property
    def input_schema(self) -> JsonSchema:
        """Expose the parameter model as the Tool input contract."""
        return self.parameters_type.model_json_schema()

    @property
    def policy(self) -> ToolPolicy:
        """Return this binding's execution limits."""
        return self.tool_policy

    @property
    def required_permissions(self) -> frozenset[str]:
        """Return grants required by this function-backed Tool."""
        return self.permissions

    async def execute(self, context: ToolContext, arguments: JsonObject) -> ToolOutput:
        """Execute the bound function with validated Tool arguments."""
        return await self.function(context, arguments)
