from datetime import UTC, datetime

from pydantic import Field

from pleiad.agent.agent import Agent
from pleiad.contracts import ContractModel, JsonObject
from pleiad.errors.base import ErrorType
from pleiad.orchestration.errors import OrchestrationStateError
from pleiad.orchestration.supervisor.supervisor import Supervisor
from pleiad.orchestration.supervisor.worker import Worker
from pleiad.runtime.lifecycle.result import RunResult
from pleiad.runtime.lifecycle.state import RunState, RunStatus
from pleiad.tools.base import FunctionTool, ToolOutput
from pleiad.tools.contracts import ToolContext, ToolDescriptor, ToolResult
from pleiad.tools.interruption import InterruptionResponse, ToolExecutionOutcome, ToolInterruption


class WorkerTaskArguments(ContractModel):
    """Model-visible input for one synchronous Worker delegation."""

    task: str = Field(min_length=1)


class SubmitWorkerArguments(ContractModel):
    """Model-visible input for asynchronous Worker submission."""

    worker_id: str = Field(min_length=1)
    task: str = Field(min_length=1)


class WaitWorkerArguments(ContractModel):
    """Model-visible input for waiting on one accepted delegation."""

    delegation_id: str = Field(min_length=1)


class DelegationTool(FunctionTool):
    """FunctionTool that can resume a paused child Run without re-submitting it."""

    def __init__(self, descriptor: ToolDescriptor, worker: Worker, supervisor: Supervisor) -> None:
        """Bind the Worker and Supervisor objects owned by the host."""
        self.worker = worker
        self.supervisor = supervisor
        super().__init__(descriptor, self.invoke, WorkerTaskArguments)

    async def invoke(self, context: ToolContext, arguments: JsonObject) -> ToolOutput:
        """Run the configured Worker and project one local result."""
        result = await self.supervisor.run_worker(self.worker, required_text(arguments["task"], "task"), context)
        if result.status is not RunStatus.INTERRUPTED or result.checkpoint is None:
            return project_worker_result(result)
        delegation_id = next(
            delegation_id
            for delegation_id, record in self.supervisor.records.items()
            if record.child_run_id == result.run_id
        )
        now = datetime.now(UTC)
        return ToolInterruption(
            call_id=context.call_id,
            tool_code=self.descriptor.code,
            tool_name=self.descriptor.name,
            arguments=arguments,
            reason="Worker Run requires host resolution.",
            details={
                "delegation_id": delegation_id,
                "child_run_id": result.run_id,
                "checkpoint": result.checkpoint.model_dump(mode="json"),
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
            raise OrchestrationStateError("A paused Worker interruption is missing its checkpoint.")
        delegation_id = interruption.details.get("delegation_id")
        if not isinstance(delegation_id, str):
            raise OrchestrationStateError("A paused Worker interruption is missing its delegation ID.")
        try:
            checkpoint = RunState.model_validate(checkpoint_value)
        except ValueError as error:
            raise OrchestrationStateError("A paused Worker checkpoint is invalid.") from error
        child_memory = self.supervisor.memory_for_interruption(interruption, self.worker)
        child_context = self.supervisor.resume_context(delegation_id, context)
        if not checkpoint.interruptions:
            raise OrchestrationStateError("A paused Worker checkpoint has no pending interruption.")
        child_interruption = checkpoint.interruptions[0]
        child_response = InterruptionResponse(
            interruption_id=child_interruption.interruption_id,
            result=response.result.model_copy(update={"call_id": child_interruption.call_id}),
        )
        child_result = await self.worker.runner.resume(
            self.worker.agent,
            checkpoint,
            memory=child_memory,
            resolutions=(child_response,),
            config=self.worker.child_config(child_context),
            handoffs=self.worker.handoffs,
            parent_run_id=context.run_id,
            parent_policy=self.worker.parent_policy(child_context),
        )
        self.supervisor.record_result(delegation_id, child_result)
        self.supervisor.settle_budget(delegation_id, child_result)
        if child_result.status is RunStatus.INTERRUPTED:
            return ToolInterruption(
                call_id=context.call_id,
                tool_code=self.descriptor.code,
                tool_name=self.descriptor.name,
                arguments=interruption.arguments,
                reason="Worker Run requires another host resolution.",
                details={
                    "delegation_id": delegation_id,
                    "child_run_id": child_result.run_id,
                    "checkpoint": child_result.checkpoint.model_dump(mode="json") if child_result.checkpoint else {},
                },
            )
        started_at = datetime.now(UTC)
        finished_at = datetime.now(UTC)
        if child_result.status is RunStatus.SUCCEEDED:
            return ToolResult(
                call_id=context.call_id,
                ok=True,
                output=project_worker_result(child_result),
                started_at=started_at,
                finished_at=finished_at,
            )
        return ToolResult(
            call_id=context.call_id,
            ok=False,
            output=project_worker_result(child_result),
            error_code=child_result.error.code if child_result.error is not None else "orchestration.worker.failed",
            error_message=child_result.error.message if child_result.error is not None else "The Worker Run failed.",
            error_type=child_result.error.type if child_result.error is not None else ErrorType.BUSINESS,
            started_at=started_at,
            finished_at=finished_at,
        )


def as_tool(
    worker: Worker | Agent,
    *,
    name: str,
    description: str,
    supervisor: Supervisor,
) -> DelegationTool:
    """Expose one pre-bound Worker as a normal synchronous model Tool."""
    selected = supervisor.worker_for(worker) if isinstance(worker, Worker) else supervisor.worker_for_agent(worker.name)
    return DelegationTool(ToolDescriptor(name=name, code=name, description=description), selected, supervisor)


def submit_worker(
    supervisor: Supervisor,
    *,
    name: str = "submit_worker",
    description: str = "Submit an allowed Worker task for asynchronous execution.",
) -> FunctionTool:
    """Create the explicit asynchronous Worker submission Tool."""

    async def execute(context: ToolContext, arguments: JsonObject) -> JsonObject:
        """Submit the selected Worker without returning a completion result."""
        return await supervisor.submit_worker(
            required_text(arguments["worker_id"], "worker_id"),
            required_text(arguments["task"], "task"),
            context,
        )

    return FunctionTool(ToolDescriptor(name=name, code=name, description=description), execute, SubmitWorkerArguments)


def wait_workers(
    supervisor: Supervisor,
    *,
    name: str = "wait_workers",
    description: str = "Wait for one submitted Worker and retrieve its controlled result.",
) -> FunctionTool:
    """Create the explicit asynchronous Worker wait Tool."""

    async def execute(context: ToolContext, arguments: JsonObject) -> JsonObject:
        """Wait for a completion event without injecting it into message history."""
        del context
        event = await supervisor.wait_worker(required_text(arguments["delegation_id"], "delegation_id"))
        return event.model_dump(mode="json")

    return FunctionTool(ToolDescriptor(name=name, code=name, description=description), execute, WaitWorkerArguments)


def project_worker_result(result: RunResult) -> JsonObject:
    """Project a child Run into controlled business output without raw messages."""
    output = result.structured_output
    if output is None:
        output = {"text": result.final_output}
    projected: JsonObject = {
        "child_run_id": result.run_id,
        "status": result.status.value,
        "output": output,
    }
    if result.error is not None:
        projected["error_code"] = result.error.code
        projected["error_message"] = result.error.message
    if result.checkpoint is not None:
        projected["checkpoint"] = result.checkpoint.model_dump(mode="json")
    return projected


def required_text(value: object, field_name: str) -> str:
    """Validate a model-provided text field at the Tool boundary."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be non-empty text")
    return value
