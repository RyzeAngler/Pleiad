import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from uuid import uuid4

from pleiad.agent.agent import Agent
from pleiad.context.selection.models import ContextBlock
from pleiad.contracts import JsonObject, JsonSchema
from pleiad.errors.base import ErrorType
from pleiad.memory.short_memory import ShortMemory
from pleiad.orchestration.graph.state import NodeResult, NodeStatus
from pleiad.runtime.config import RunConfig
from pleiad.runtime.execution.control import RuntimeHandoffBinding
from pleiad.runtime.execution.runner import Runner
from pleiad.runtime.lifecycle.state import RunStatus
from pleiad.runtime.policy import EffectivePolicy
from pleiad.schemas import validate_json_schema

NodeHandler = Callable[[JsonObject], Awaitable[JsonObject]]
MemoryFactory = Callable[[str], ShortMemory]


def default_node_memory(node_run_id: str) -> ShortMemory:
    """Create process-local memory for one Agent graph node Run."""
    return ShortMemory.in_memory(f"graph-node:{node_run_id}")


@dataclass(frozen=True, slots=True)
class Node:
    """One executable graph node, either a normal async function or an Agent Run."""

    node_id: str
    input_schema: JsonSchema = field(default_factory=lambda: {"type": "object"})
    output_schema: JsonSchema = field(default_factory=lambda: {"type": "object"})
    handler: NodeHandler | None = None
    agent: Agent | None = None
    runner: Runner | None = None
    memory_factory: MemoryFactory = default_node_memory
    config: RunConfig = field(default_factory=RunConfig)
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

    async def run(self, input_value: JsonObject) -> NodeResult:
        """Execute this node through its bound function or the existing Runner."""
        node_run_id = uuid4().hex
        if self.handler is not None:
            output = await self.handler(input_value)
            return NodeResult(node_id=self.node_id, node_run_id=node_run_id, status=NodeStatus.SUCCEEDED, output=output)
        if self.agent is None or self.runner is None:
            raise RuntimeError("Node executable binding is incomplete")
        result = await self.runner.run(
            self.agent,
            json.dumps(input_value, ensure_ascii=False, sort_keys=True),
            memory=self.memory_factory(node_run_id),
            config=self.config,
            handoffs=self.handoffs,
            parent_run_id=self.parent_run_id,
            parent_policy=self.parent_policy,
            context_blocks=self.context_blocks,
        )
        if result.status is RunStatus.SUCCEEDED:
            output = result.structured_output or {"text": result.final_output}
            return NodeResult(
                node_id=self.node_id,
                node_run_id=result.run_id,
                status=NodeStatus.SUCCEEDED,
                output=output,
                run_status=result.status,
                usage=result.usage,
            )
        if result.error is not None and result.error.type is ErrorType.BUSINESS:
            return NodeResult(
                node_id=self.node_id,
                node_run_id=result.run_id,
                status=NodeStatus.FAILED,
                error_code=result.error.code,
                error_message=result.error.message,
                error_type=ErrorType.BUSINESS,
                run_status=result.status,
                usage=result.usage,
            )
        if result.status in {RunStatus.LIMIT_REACHED, RunStatus.TIMED_OUT}:
            message = (
                "The Agent Node exhausted its model-call or output budget."
                if result.status is RunStatus.LIMIT_REACHED
                else "The Agent Node reached its deadline."
            )
            return NodeResult(
                node_id=self.node_id,
                node_run_id=result.run_id,
                status=NodeStatus.FAILED,
                error_code=f"orchestration.graph.node_{result.status.value}",
                error_message=message,
                error_type=ErrorType.BUSINESS,
                run_status=result.status,
                usage=result.usage,
            )
        raise RuntimeError("Agent graph node did not complete as a business result")
