from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from pleiad.budgets import BudgetLedger
from pleiad.contracts import ContractModel, JsonObject, JsonSchema
from pleiad.models.contracts import Tool
from pleiad.schemas import validate_json_schema
from pleiad.tools.contracts import ToolAcceptance, ToolContext, ToolDescriptor, ToolPolicy, ToolSubmission
from pleiad.tools.errors import ToolError, ToolSystemFault
from pleiad.tools.interruption import InterruptionResponse, ToolExecutionOutcome, ToolInterruption


class BackgroundAdmission:
    """Own one stable action identity and its durable concrete backend acceptance."""

    def __init__(
        self,
        operation_id: str,
        *,
        on_prepare: Callable[[ToolSubmission], Awaitable[None]],
        on_accept: Callable[[ToolAcceptance], Awaitable[None]],
    ) -> None:
        """Bind the actual acceptance writer supplied by the Run's operation owner."""
        self.operation_id = operation_id
        self.on_prepare = on_prepare
        self.on_accept = on_accept
        self.submission: ToolSubmission | None = None
        self.prepared = False
        self.receipt: ToolAcceptance | None = None
        self.entered = False

    async def prepare(self, submission: ToolSubmission) -> None:
        """Persist the exact resolved request before permitting backend submission."""
        if submission.operation_id != self.operation_id:
            raise ValueError("Backend submission must retain its stable operation identity")
        if self.submission is not None and self.submission != submission:
            raise ValueError("A prepared backend request cannot change")
        if self.prepared:
            return
        self.submission = submission
        await self.on_prepare(submission)
        self.prepared = True

    async def accept(self, receipt: ToolAcceptance) -> None:
        """Persist one immutable receipt before the backend proceeds with execution."""
        if not self.prepared or self.submission is None:
            raise ValueError("Backend acceptance requires a durably prepared request")
        if receipt.request_digest != self.submission.request_digest:
            raise ValueError("Backend acceptance must match the prepared request digest")
        if receipt.operation_id != self.operation_id:
            raise ValueError("Backend acceptance must retain its stable operation identity")
        if self.receipt is not None:
            if self.receipt != receipt:
                raise ValueError("An accepted backend receipt cannot change")
            return
        self.receipt = receipt
        await self.on_accept(receipt)


@dataclass(frozen=True, slots=True)
class ToolScope:
    """Bind actual reusable resource ownership to the invoking Run lifecycle."""

    open: Callable[[str], Awaitable[None]]
    wait: Callable[[str], Awaitable[None]]
    close: Callable[[str, bool, bool], Awaitable[None]]
    checkpoint: Callable[[str], JsonObject | None]
    restore: Callable[[str, JsonObject], None]


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

    @property
    def budget_ledger(self) -> BudgetLedger | None:
        """Return the host-owned shared reservation owner for scoped child execution."""
        return None

    @property
    def execution_scope(self) -> ToolScope | None:
        """Return live resource ownership that the invoking Run must settle."""
        return None

    @property
    def supports_background(self) -> bool:
        """Declare whether this implementation has a concrete backend acceptance contract."""
        return False

    async def execute_background(
        self, context: ToolContext, arguments: JsonObject, admission: BackgroundAdmission
    ) -> ToolOutput:
        """Reject background execution unless the backend implements actual admission."""
        raise ToolSystemFault("This Tool has no background backend acceptance contract.")

    async def authorize(self, context: ToolContext, arguments: JsonObject) -> ToolInterruption | None:
        """Request host authorization before admitting any external backend action."""
        return None

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
        budget_ledger: BudgetLedger | None = None,
        execution_scope: ToolScope | None = None,
    ) -> None:
        """Bind a function, validated parameter model, and host execution policy."""
        self.tool_descriptor = descriptor
        self.function = function
        self.parameters_type = parameters_type
        self.tool_policy = ToolPolicy() if policy is None else policy
        if any(not permission.strip() for permission in required_permissions):
            raise ValueError("Tool permissions must be non-empty strings")
        self.permissions = frozenset(required_permissions)
        self.bound_budget = budget_ledger
        self.bound_scope = execution_scope
        validate_json_schema(self.input_schema)

    @property
    def budget_ledger(self) -> BudgetLedger | None:
        """Expose the concrete child ledger supplied by the host binding."""
        return self.bound_budget

    @property
    def execution_scope(self) -> ToolScope | None:
        """Expose the resource scope supplied by the host binding."""
        return self.bound_scope

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
