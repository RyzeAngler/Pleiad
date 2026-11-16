from pydantic import Field

from pleiad.contracts import ContractModel, JsonObject
from pleiad.orchestration.collaboration.contracts import (
    CloseRequest,
    EventCursor,
    InterruptRequest,
    MessageRequest,
    SpawnRequest,
)
from pleiad.orchestration.collaboration.endpoint import MemberEndpoint
from pleiad.tools.base import FunctionTool
from pleiad.tools.contracts import ToolContext, ToolDescriptor, ToolPolicy


class ListArguments(ContractModel):
    """List only membership visible within the bound scope."""


class StatusArguments(ContractModel):
    """Read bounded work metadata for an authorized descendant."""

    member_id: str = Field(min_length=1)


class ResultArguments(ContractModel):
    """Select one completed work without consuming an event cursor."""

    work_id: str = Field(min_length=1)


class WaitArguments(ContractModel):
    """Wait for any descendant update after an independent observer cursor."""

    member_ids: tuple[str, ...]
    after: EventCursor
    timeout: float = Field(default=30.0, ge=0, le=60)
    limit: int = Field(default=100, ge=1, le=1000)


def collaboration_tools(endpoint: MemberEndpoint) -> tuple[FunctionTool, ...]:
    """Expose collaboration commands with caller identity bound by the host.

    At least one returned Tool must be declared on a member Agent so Runner can
    attach its lifecycle. The host chooses which commands each profile receives.
    """

    async def spawn(context: ToolContext, arguments: JsonObject) -> JsonObject:
        return (await endpoint.spawn(SpawnRequest.model_validate(arguments), context)).model_dump(mode="json")

    async def send(context: ToolContext, arguments: JsonObject) -> JsonObject:
        return (await endpoint.send_message(MessageRequest.model_validate(arguments), context)).model_dump(mode="json")

    async def followup(context: ToolContext, arguments: JsonObject) -> JsonObject:
        return (await endpoint.followup(MessageRequest.model_validate(arguments), context)).model_dump(mode="json")

    async def interrupt(context: ToolContext, arguments: JsonObject) -> JsonObject:
        return (await endpoint.interrupt(InterruptRequest.model_validate(arguments), context)).model_dump(mode="json")

    async def close(context: ToolContext, arguments: JsonObject) -> JsonObject:
        return (await endpoint.close_member(CloseRequest.model_validate(arguments), context)).model_dump(mode="json")

    async def members(context: ToolContext, arguments: JsonObject) -> JsonObject:
        endpoint.group.authorize(endpoint.member_id, context)
        ListArguments.model_validate(arguments)
        return {"members": [value.model_dump(mode="json") for value in await endpoint.list_members()]}

    async def status(context: ToolContext, arguments: JsonObject) -> JsonObject:
        endpoint.group.authorize(endpoint.member_id, context)
        request = StatusArguments.model_validate(arguments)
        works = await endpoint.status(request.member_id)
        return {
            "works": [
                {
                    "work_id": value.proposal.work_id,
                    "run_id": value.run_id,
                    "phase": value.phase,
                    "resolution": value.resolution,
                    "reason": value.reason,
                }
                for value in works
            ]
        }

    async def result(context: ToolContext, arguments: JsonObject) -> JsonObject:
        endpoint.group.authorize(endpoint.member_id, context)
        request = ResultArguments.model_validate(arguments)
        receipt = await endpoint.read_result(request.work_id)
        return {"result": None if receipt is None else receipt.model_dump(mode="json")}

    async def wait(context: ToolContext, arguments: JsonObject) -> JsonObject:
        endpoint.group.authorize(endpoint.member_id, context)
        request = WaitArguments.model_validate(arguments)
        return (
            await endpoint.wait(
                request.member_ids, after=request.after, timeout_seconds=request.timeout, limit=request.limit
            )
        ).model_dump(mode="json")

    return tuple(
        FunctionTool(
            ToolDescriptor(name=name, code=name, description=description),
            function,
            parameters,
            policy=ToolPolicy(timeout_seconds=65.0, parallel_safe=True),
            budget_ledger=endpoint.group.budgets,
            execution_scope=endpoint.execution_scope,
        )
        for name, description, function, parameters in (
            ("spawn_agent", "Create an independent member from an allowed profile.", spawn, SpawnRequest),
            ("send_message", "Deliver input without starting an idle member.", send, MessageRequest),
            ("followup_task", "Append to an active Run or start new work on an idle member.", followup, MessageRequest),
            ("interrupt_agent", "Interrupt only the exact observed work and Run.", interrupt, InterruptRequest),
            ("close_agent", "Close a member subtree and settle its owned work.", close, CloseRequest),
            ("list_agents", "Read members in this collaboration scope.", members, ListArguments),
            ("agent_status", "Read an authorized descendant's work states.", status, StatusArguments),
            ("read_agent_result", "Read the original completed work result.", result, ResultArguments),
            (
                "wait_agents",
                "Observe updates from any selected descendant without consuming them.",
                wait,
                WaitArguments,
            ),
        )
    )
