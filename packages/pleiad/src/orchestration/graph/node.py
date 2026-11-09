import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from uuid import uuid4

from pleiad.agent.agent import Agent
from pleiad.context.selection.models import ContextBlock
from pleiad.contracts import JsonObject, JsonSchema
from pleiad.errors.base import ErrorType
from pleiad.orchestration.graph.state import NodeResult, NodeStatus
from pleiad.runtime.checkpoint import ResumeDecision
from pleiad.runtime.execution.control import RuntimeHandoffBinding
from pleiad.runtime.limits import RunLimits
from pleiad.runtime.policy import EffectivePolicy
from pleiad.runtime.result import RunSuspension
from pleiad.runtime.runner import Runner
from pleiad.schemas import validate_json_schema
from pleiad.sessions.backends.in_memory import InMemorySessionStore
from pleiad.sessions.checkpoint import RunCheckpoint
from pleiad.sessions.input import InputEnvelope
from pleiad.sessions.records import RunStatus
from pleiad.sessions.session import Session
from pleiad.sessions.store import SessionStore

NodeHandler = Callable[[JsonObject], Awaitable[JsonObject]]


@dataclass(frozen=True, slots=True)
class Node:
    """Bind one graph function or Agent invocation and its session storage."""

    node_id: str
    input_schema: JsonSchema = field(default_factory=lambda: {"type": "object"})
    output_schema: JsonSchema = field(default_factory=lambda: {"type": "object"})
    handler: NodeHandler | None = None
    agent: Agent | None = None
    runner: Runner | None = None
    session_store: SessionStore = field(default_factory=InMemorySessionStore)
    limits: RunLimits = field(default_factory=RunLimits)
    handoffs: tuple[RuntimeHandoffBinding, ...] = ()
    side_effect: bool = False
    parent_run_id: str | None = None
    parent_policy: EffectivePolicy | None = None
    context_blocks: tuple[ContextBlock, ...] = ()

    def __post_init__(self) -> None:
        """Validate one node's executable binding and schemas."""
        if not self.node_id.strip():
            raise ValueError("node_id must not be blank")
        has_function = self.handler is not None
        has_agent = self.agent is not None or self.runner is not None
        if has_function == has_agent:
            raise ValueError("a Node must bind exactly one handler or Agent/Runner pair")
        if has_agent and (self.agent is None or self.runner is None):
            raise ValueError("an Agent Node requires both Agent and Runner")
        validate_json_schema(self.input_schema)
        validate_json_schema(self.output_schema)

    async def run(
        self,
        input_value: JsonObject,
        *,
        checkpoint: RunCheckpoint | None = None,
        decisions: tuple[ResumeDecision, ...] = (),
    ) -> NodeResult:
        """Start or resume this node while preserving its SDK Run identity.

        Args:
            input_value: Validated node input selected by the graph.
            checkpoint: The exact paused Run checkpoint when resuming an Agent.
            decisions: Host resolutions for requests in that checkpoint.

        Returns:
            A confirmed terminal result or a typed paused node result.

        Raises:
            ValueError: If a function node receives an Agent checkpoint.
        """
        invocation_id = uuid4().hex
        if self.handler is not None:
            if checkpoint is not None or decisions:
                raise ValueError("Function nodes do not accept Agent resume decisions")
            output = await self.handler(input_value)
            return NodeResult(
                node_id=self.node_id, node_run_id=invocation_id, status=NodeStatus.SUCCEEDED, output=output
            )
        if self.agent is None or self.runner is None:
            raise RuntimeError("Node executable binding is incomplete")
        if checkpoint is None:
            if decisions:
                raise ValueError("Node resume decisions require a checkpoint")
            session = Session(f"graph-node:{invocation_id}", self.session_store)
            input_id = f"{invocation_id}:input"
            await session.enqueue(
                InputEnvelope(
                    input_id=input_id,
                    kind="user",
                    source=f"graph:{self.node_id}",
                    content=json.dumps(input_value, ensure_ascii=False, sort_keys=True),
                )
            )
            handle = await self.runner.start(
                self.agent,
                session=session,
                invocation_id=invocation_id,
                input_ids=(input_id,),
                input_mode="batch",
                limits=self.limits,
                handoffs=self.handoffs,
                parent_run_id=self.parent_run_id,
                parent_policy=self.parent_policy,
                context=self.context_blocks,
            )
        else:
            session = Session(checkpoint.session_id, self.session_store)
            handle = await self.runner.resume(
                self.agent,
                session=session,
                checkpoint=checkpoint,
                decisions=decisions,
                limits=self.limits,
                handoffs=self.handoffs,
                parent_policy=self.parent_policy,
            )
        async with handle:
            result = await handle.result()
        if isinstance(result, RunSuspension):
            return NodeResult(
                node_id=self.node_id,
                node_run_id=result.run_id,
                session_id=session.session_id,
                status=NodeStatus.PAUSED,
                run_status=result.state,
                usage=result.usage,
                checkpoint=result.checkpoint,
            )
        journal = await session.store.snapshot(session.session_id)
        unresolved = any(
            item.run_id == result.run_id and item.state in {"prepared", "running", "unknown"}
            for item in journal.tool_calls
        )
        if unresolved:
            return NodeResult(
                node_id=self.node_id,
                node_run_id=result.run_id,
                session_id=session.session_id,
                status=NodeStatus.UNKNOWN,
                error_code="orchestration.graph.recovery_unknown",
                error_message="The Agent Node has external actions requiring host reconciliation.",
                error_type=ErrorType.BUSINESS,
                run_status=result.state,
                usage=result.usage,
            )
        if result.state is RunStatus.CANCELLED:
            return NodeResult(
                node_id=self.node_id,
                node_run_id=result.run_id,
                session_id=session.session_id,
                status=NodeStatus.CANCELLED,
                run_status=result.state,
                usage=result.usage,
            )
        if result.state is RunStatus.SUCCEEDED:
            result_output = result.structured_output
            if result_output is None:
                result_output = {"text": result.final_output}
            return NodeResult(
                node_id=self.node_id,
                node_run_id=result.run_id,
                session_id=session.session_id,
                status=NodeStatus.SUCCEEDED,
                output=result_output,
                run_status=result.state,
                usage=result.usage,
            )
        return NodeResult(
            node_id=self.node_id,
            node_run_id=result.run_id,
            session_id=session.session_id,
            status=NodeStatus.FAILED,
            error_code=result.error.code if result.error is not None else f"orchestration.graph.node_{result.reason}",
            error_message=result.error.message
            if result.error is not None
            else f"The Agent Node stopped: {result.reason}.",
            error_type=ErrorType.BUSINESS,
            run_status=result.state,
            usage=result.usage,
        )
