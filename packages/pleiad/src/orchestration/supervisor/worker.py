from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from pleiad.agent.agent import Agent
from pleiad.context.selection.models import ContextBlock
from pleiad.memory.short_memory import ShortMemory
from pleiad.orchestration.budget import child_policy
from pleiad.orchestration.errors import WorkerPolicyError
from pleiad.runtime.config import RunConfig
from pleiad.runtime.execution.control import RuntimeHandoffBinding
from pleiad.runtime.execution.runner import Runner
from pleiad.runtime.policy import EffectivePolicy
from pleiad.tools.contracts import ToolContext


def default_memory_factory(delegation_id: str) -> ShortMemory:
    """Create an isolated process-local memory stream for one Worker Run."""
    return ShortMemory.in_memory(f"worker:{delegation_id}")


@dataclass(frozen=True, slots=True)
class Worker:
    """Bind one independent Agent Run and its host-selected resources."""

    worker_id: str
    agent: Agent
    runner: Runner
    memory_factory: Callable[[str], ShortMemory] = default_memory_factory
    config: RunConfig = field(default_factory=RunConfig)
    context_selector: Callable[[str], tuple[ContextBlock, ...]] | None = None
    handoffs: tuple[RuntimeHandoffBinding, ...] = ()

    def __post_init__(self) -> None:
        """Validate the stable Worker identity."""
        if not self.worker_id.strip():
            raise ValueError("worker_id must not be blank")

    def memory(self, delegation_id: str) -> ShortMemory:
        """Create the independent ShortMemory selected by the host."""
        return self.memory_factory(delegation_id)

    def context_blocks(self, task: str) -> tuple[ContextBlock, ...]:
        """Select task-specific context without copying the parent history."""
        if self.context_selector is None:
            return ()
        return self.context_selector(task)

    def child_config(self, context: ToolContext) -> RunConfig:
        """Intersect the Worker request with grants visible to the parent Tool call."""
        grants = context.grants if self.config.grants is None else context.grants.intersect(self.config.grants)
        return RunConfig.model_validate({**self.config.model_dump(), "grants": grants})

    def parent_policy(self, context: ToolContext) -> EffectivePolicy | None:
        """Project validated parent limits into the Runtime child-policy boundary."""
        limits = context.parent_limits
        if limits is None:
            return None
        now = datetime.now(UTC)
        remaining_seconds = (limits.deadline - now).total_seconds()
        if remaining_seconds <= 0:
            raise WorkerPolicyError("The parent Run deadline expired before Worker execution.")
        return child_policy(context)
