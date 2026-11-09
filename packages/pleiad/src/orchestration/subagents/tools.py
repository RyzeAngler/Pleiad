from datetime import UTC, datetime

from pydantic import Field

from pleiad.agent.agent import Agent
from pleiad.contracts import ContractModel, JsonObject
from pleiad.errors.base import ErrorType
from pleiad.orchestration.errors import OrchestrationStateError
from pleiad.orchestration.subagents.group import SubagentGroup
from pleiad.orchestration.subagents.state import SubagentSnapshot
from pleiad.orchestration.subagents.subagent import Subagent
from pleiad.runtime.checkpoint import ResumeDecision
from pleiad.runtime.result import RunOutcome, RunSuspension
from pleiad.sessions.checkpoint import RunCheckpoint
from pleiad.sessions.records import RunStatus
from pleiad.tools.base import FunctionTool, ToolOutput
from pleiad.tools.contracts import ToolContext, ToolDescriptor, ToolPolicy, ToolResult
from pleiad.tools.interruption import InterruptionResponse, ToolExecutionOutcome, ToolInterruption


class SubagentTaskArguments(ContractModel):
    """Model-visible input for one synchronous Subagent delegation."""

    task: str = Field(min_length=1)


class SubmitSubagentArguments(ContractModel):
    """Model-visible input for asynchronous Subagent submission."""

    subagent_id: str = Field(min_length=1)
    task: str = Field(min_length=1)


class WaitSubagentArguments(ContractModel):
    """Model-visible input for waiting on one accepted delegation."""

    delegation_id: str = Field(min_length=1)


class DelegationTool(FunctionTool):
    """FunctionTool that can resume a paused child Run without re-submitting it."""

    def __init__(self, descriptor: ToolDescriptor, subagent: Subagent, group: SubagentGroup) -> None:
        """Bind the Subagent and SubagentGroup objects owned by the host."""
        self.subagent = subagent
        self.group = group
        super().__init__(
            descriptor,
            self.invoke,
            SubagentTaskArguments,
            policy=ToolPolicy(parallel_safe=True),
            budget_ledger=group.budgets,
            execution_scope=group.execution_scope,
        )

    async def invoke(self, context: ToolContext, arguments: JsonObject) -> ToolOutput:
        """Run the configured Subagent and project one local result."""
        result = await self.group.run_subagent(self.subagent, required_text(arguments["task"], "task"), context)
        if not isinstance(result, RunSuspension):
            return project_subagent_result(result)
        delegation_id = next(
            delegation_id
            for delegation_id, record in self.group.records.items()
            if record.child_run_id == result.run_id
        )
        now = datetime.now(UTC)
        return ToolInterruption(
            call_id=context.call_id,
            tool_code=self.descriptor.code,
            tool_name=self.descriptor.name,
            arguments=arguments,
            reason="Subagent Run requires host resolution.",
            details={
                "kind": "pleiad.subagent",
                "delegation_id": delegation_id,
                "child_run_id": result.run_id,
                "checkpoint": result.checkpoint.model_dump(mode="json"),
                "group_snapshot": self.group.snapshot().model_dump(mode="json"),
                "created_at": now.isoformat(),
            },
        )

    async def resume_interruption(
        self,
        context: ToolContext,
        interruption: ToolInterruption,
        response: InterruptionResponse,
    ) -> ToolExecutionOutcome:
        """Resume the exact child checkpoint and keep the parent Tool call ID."""
        checkpoint_value = interruption.details.get("checkpoint")
        if not isinstance(checkpoint_value, dict):
            raise OrchestrationStateError("A paused Subagent interruption is missing its checkpoint.")
        delegation_id = interruption.details.get("delegation_id")
        if not isinstance(delegation_id, str):
            raise OrchestrationStateError("A paused Subagent interruption is missing its delegation ID.")
        try:
            checkpoint = RunCheckpoint.model_validate(checkpoint_value)
        except ValueError as error:
            raise OrchestrationStateError("A paused Subagent checkpoint is invalid.") from error
        if delegation_id not in self.group.records:
            snapshot_value = interruption.details.get("group_snapshot")
            if not isinstance(snapshot_value, dict):
                raise OrchestrationStateError("A paused Subagent is missing its group checkpoint.")
            snapshot = SubagentSnapshot.model_validate(snapshot_value)
            self.group.restore(snapshot)
        child_session = self.group.session_for_interruption(interruption, self.subagent)
        child_context = self.group.resume_context(delegation_id, context)
        output = response.result.output
        decisions_value = None if output is None else output.get("subagent_decisions")
        if not isinstance(decisions_value, list):
            raise OrchestrationStateError("A Subagent resolution requires explicit child decisions.")
        try:
            decisions = tuple(ResumeDecision.model_validate(value) for value in decisions_value)
        except ValueError as error:
            raise OrchestrationStateError("The Subagent decisions are invalid.") from error
        handle = await self.subagent.runner.resume(
            self.subagent.agent,
            session=child_session,
            checkpoint=checkpoint,
            decisions=decisions,
            limits=self.subagent.child_limits(child_context),
            handoffs=self.subagent.handoffs,
            parent_policy=self.subagent.parent_policy(child_context),
        )
        async with handle:
            child_result = await handle.result()
        self.group.record_result(delegation_id, child_result)
        self.group.settle_budget(delegation_id, child_result)
        if isinstance(child_result, RunSuspension):
            return ToolInterruption(
                call_id=context.call_id,
                tool_code=self.descriptor.code,
                tool_name=self.descriptor.name,
                arguments=interruption.arguments,
                reason="Subagent Run requires another host resolution.",
                details={
                    "kind": "pleiad.subagent",
                    "delegation_id": delegation_id,
                    "child_run_id": child_result.run_id,
                    "checkpoint": child_result.checkpoint.model_dump(mode="json"),
                    "group_snapshot": self.group.snapshot().model_dump(mode="json"),
                },
            )
        started_at = datetime.now(UTC)
        finished_at = datetime.now(UTC)
        if child_result.state is RunStatus.SUCCEEDED:
            return ToolResult(
                call_id=context.call_id,
                ok=True,
                output=project_subagent_result(child_result),
                started_at=started_at,
                finished_at=finished_at,
            )
        return ToolResult(
            call_id=context.call_id,
            ok=False,
            output=project_subagent_result(child_result),
            error_code=child_result.error.code if child_result.error is not None else "orchestration.subagent.failed",
            error_message=child_result.error.message if child_result.error is not None else "The Subagent Run failed.",
            error_type=child_result.error.type if child_result.error is not None else ErrorType.BUSINESS,
            started_at=started_at,
            finished_at=finished_at,
        )


def as_tool(
    subagent: Subagent | Agent,
    *,
    name: str,
    description: str,
    group: SubagentGroup,
) -> DelegationTool:
    """Expose one pre-bound Subagent as a normal synchronous model Tool."""
    selected = (
        group.subagent_for(subagent) if isinstance(subagent, Subagent) else group.subagent_for_agent(subagent.name)
    )
    return DelegationTool(ToolDescriptor(name=name, code=name, description=description), selected, group)


def submit_subagent(
    group: SubagentGroup,
    *,
    name: str = "submit_subagent",
    description: str = "Submit an allowed Subagent task for asynchronous execution.",
) -> FunctionTool:
    """Create the explicit asynchronous Subagent submission Tool."""

    async def execute(context: ToolContext, arguments: JsonObject) -> JsonObject:
        """Submit the selected Subagent without returning a completion result."""
        return await group.submit_subagent(
            required_text(arguments["subagent_id"], "subagent_id"),
            required_text(arguments["task"], "task"),
            context,
        )

    return FunctionTool(
        ToolDescriptor(name=name, code=name, description=description),
        execute,
        SubmitSubagentArguments,
        budget_ledger=group.budgets,
        execution_scope=group.execution_scope,
    )


def wait_subagents(
    group: SubagentGroup,
    *,
    name: str = "wait_subagents",
    description: str = "Wait for one submitted Subagent and retrieve its controlled result.",
) -> FunctionTool:
    """Create the explicit asynchronous Subagent wait Tool."""

    async def execute(context: ToolContext, arguments: JsonObject) -> JsonObject:
        """Wait for a completion event without injecting it into message history."""
        group.bind_owner(context)
        event = await group.wait_subagent(required_text(arguments["delegation_id"], "delegation_id"))
        return event.model_dump(mode="json")

    return FunctionTool(
        ToolDescriptor(name=name, code=name, description=description),
        execute,
        WaitSubagentArguments,
        budget_ledger=group.budgets,
        execution_scope=group.execution_scope,
    )


def project_subagent_result(result: RunOutcome) -> JsonObject:
    """Project a child outcome into controlled output without its private history."""
    projected: JsonObject = {"child_run_id": result.run_id, "state": result.state.value}
    if isinstance(result, RunSuspension):
        projected["checkpoint"] = result.checkpoint.model_dump(mode="json")
        return projected
    projected["output"] = (
        result.structured_output if result.structured_output is not None else {"text": result.final_output}
    )
    projected["reason"] = result.reason
    if result.error is not None:
        projected["error_code"] = result.error.code
        projected["error_message"] = result.error.message
    return projected


def required_text(value: object, field_name: str) -> str:
    """Validate a model-provided text field at the Tool boundary."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be non-empty text")
    return value
