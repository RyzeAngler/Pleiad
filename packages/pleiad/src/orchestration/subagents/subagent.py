from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from pleiad.agent.agent import Agent
from pleiad.context.selection.models import ContextBlock
from pleiad.orchestration.budget import child_policy
from pleiad.orchestration.errors import SubagentPolicyError
from pleiad.runtime.execution.control import RuntimeHandoffBinding
from pleiad.runtime.limits import RunLimits
from pleiad.runtime.policy import EffectivePolicy, effective_policy
from pleiad.runtime.runner import Runner
from pleiad.sessions.backends.in_memory import InMemorySessionStore
from pleiad.sessions.session import Session
from pleiad.sessions.store import SessionStore
from pleiad.tools.contracts import ToolContext


@dataclass(frozen=True, slots=True)
class Subagent:
    """Bind one scoped Agent invocation and its host-selected resources."""

    subagent_id: str
    agent: Agent
    runner: Runner
    session_store: SessionStore = field(default_factory=InMemorySessionStore)
    limits: RunLimits = field(default_factory=RunLimits)
    context_selector: Callable[[str], tuple[ContextBlock, ...]] | None = None
    handoffs: tuple[RuntimeHandoffBinding, ...] = ()

    def __post_init__(self) -> None:
        """Validate the stable Subagent identity."""
        if not self.subagent_id.strip():
            raise ValueError("subagent_id must not be blank")

    def session(self, delegation_id: str) -> Session:
        """Open the isolated Session belonging to this scoped child invocation."""
        return Session(f"subagent:{delegation_id}", self.session_store)

    def context_blocks(self, task: str) -> tuple[ContextBlock, ...]:
        """Select task-specific context without copying the parent history."""
        if self.context_selector is None:
            return ()
        return self.context_selector(task)

    def child_limits(self, context: ToolContext) -> RunLimits:
        """Intersect the Subagent request with grants visible to the parent Tool call."""
        grants = context.grants if self.limits.grants is None else context.grants.intersect(self.limits.grants)
        return RunLimits.model_validate({**self.limits.model_dump(), "grants": grants})

    def parent_policy(self, context: ToolContext) -> EffectivePolicy:
        """Apply inherited allowances or the actual host-bound Subagent limits."""
        parent = child_policy(context)
        if parent is None:
            return effective_policy(
                self.child_limits(context), self.runner.host_policy, agent_hints=self.agent.policy_hints
            )
        if parent.deadline <= datetime.now(UTC):
            raise SubagentPolicyError("The parent Run deadline expired before Subagent execution.")
        return parent
