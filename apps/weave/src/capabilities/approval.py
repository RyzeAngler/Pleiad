from pleiad.contracts import JsonObject, JsonSchema
from pleiad.tools.base import BaseTool, ToolOutput
from pleiad.tools.builtins.read_file import ReadFileTool
from pleiad.tools.contracts import ToolCall, ToolContext, ToolDescriptor, ToolPolicy
from pleiad.tools.interruption import InterruptionResponse, ToolExecutionOutcome, ToolInterruption
from pleiad.tools.registry import ToolRegistry
from pleiad.tools.runtime import ToolRuntime


class RequiresApprovalTool(BaseTool):
    """Defer a host-selected Tool until its exact invocation is approved."""

    def __init__(self, tool: BaseTool) -> None:
        """Retain the real implementation and reuse SDK execution validation."""
        self.tool = tool
        self.execution_runtime = ToolRuntime()

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

    async def execute(self, context: ToolContext, arguments: JsonObject) -> ToolOutput:
        """Request approval before invoking the implementation."""
        return ToolInterruption(
            call_id=context.call_id,
            tool_code=self.descriptor.code,
            tool_name=self.descriptor.name,
            arguments=arguments,
            reason="The workspace owner must approve this Tool invocation.",
            details={"kind": "weave.approval"},
        )

    async def resume_interruption(
        self, context: ToolContext, interruption: ToolInterruption, response: InterruptionResponse
    ) -> ToolExecutionOutcome:
        """Execute an approved action once or return its server-authored denial."""
        if not response.result.ok:
            return response.result
        return await self.execution_runtime.execute(
            self.tool,
            ToolCall(call_id=context.call_id, tool_code=self.descriptor.code, arguments=interruption.arguments),
            context,
        )


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
