from pleiad.budgets import BudgetLedger
from pleiad.contracts import JsonObject, JsonSchema
from pleiad.tools.base import BackgroundAdmission, BaseTool, ToolOutput, ToolScope
from pleiad.tools.builtins.read_file import ReadFileTool
from pleiad.tools.contracts import ToolContext, ToolDescriptor, ToolPolicy
from pleiad.tools.interruption import InterruptionResponse, ToolExecutionOutcome, ToolInterruption
from pleiad.tools.registry import ToolRegistry


class RequiresApprovalTool(BaseTool):
    """Defer a host-selected Tool until its exact invocation is approved."""

    def __init__(self, tool: BaseTool) -> None:
        """Retain the real implementation and reuse SDK execution validation."""
        self.tool = tool

    @property
    def descriptor(self) -> ToolDescriptor:
        """Preserve the pending Tool's stable identity."""
        return self.tool.descriptor

    @property
    def input_schema(self) -> JsonSchema:
        """Preserve the implementation's input contract."""
        return self.tool.input_schema

    @property
    def policy(self) -> ToolPolicy:
        """Apply the implementation's concurrency and timeout limits."""
        return self.tool.policy

    @property
    def required_permissions(self) -> frozenset[str]:
        """Retain the underlying permission checks before and after approval."""
        return self.tool.required_permissions

    @property
    def budget_ledger(self) -> BudgetLedger | None:
        """Preserve the actual scoped budget owner across the approval boundary."""
        return self.tool.budget_ledger

    @property
    def execution_scope(self) -> ToolScope | None:
        """Retain child resource lifecycle ownership while approvals remain pending."""
        return self.tool.execution_scope

    @property
    def supports_background(self) -> bool:
        """Preserve the implementation's concrete backend admission capability."""
        return self.tool.supports_background

    async def authorize(self, context: ToolContext, arguments: JsonObject) -> ToolInterruption:
        """Request host authorization before runtime action or backend admission."""
        return ToolInterruption(
            call_id=context.call_id,
            tool_code=self.descriptor.code,
            tool_name=self.descriptor.name,
            arguments=arguments,
            reason="The workspace owner must approve this Tool invocation.",
            details={"kind": "weave.approval"},
        )

    async def execute(self, context: ToolContext, arguments: JsonObject) -> ToolOutput:
        """Execute the original implementation after the SDK authorizes this action."""
        return await self.tool.execute(context, arguments)

    async def execute_background(
        self, context: ToolContext, arguments: JsonObject, admission: BackgroundAdmission
    ) -> ToolOutput:
        """Retain actual backend acceptance under the same SDK operation identity."""
        return await self.tool.execute_background(context, arguments, admission)

    async def resume_interruption(
        self, context: ToolContext, interruption: ToolInterruption, response: InterruptionResponse
    ) -> ToolExecutionOutcome:
        """Delegate native execution interruptions after SDK-owned authorization."""
        return await self.tool.resume_interruption(context, interruption, response)


class RequiresApprovalReadFileTool(RequiresApprovalTool, ReadFileTool):
    """Preserve the SDK's real Skill-root binding while approving file reads."""

    def __init__(self, tool: ReadFileTool) -> None:
        """Retain the validated reader binding without copying filesystem behavior."""
        ReadFileTool.__init__(self, tool.descriptor, tool.binding)
        RequiresApprovalTool.__init__(self, tool)


def with_approvals(source: ToolRegistry, codes: frozenset[str]) -> ToolRegistry:
    """Bind explicit host approvals without changing SDK or source registrations."""
    if not codes:
        return source
    for code in codes:
        source.resolve(code)
    registry = ToolRegistry()
    for code in source.codes():

        def create(descriptor: ToolDescriptor, selected_code: str = code) -> BaseTool:
            """Construct one original Tool with its selected approval policy."""
            tool = source.load(selected_code)
            if selected_code not in codes or isinstance(tool, RequiresApprovalTool):
                return tool
            if isinstance(tool, ReadFileTool):
                return RequiresApprovalReadFileTool(tool)
            return RequiresApprovalTool(tool)

        registry.register(source.resolve(code), create)
    return registry
