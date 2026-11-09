from dataclasses import dataclass
from datetime import UTC, datetime

from pleiad.agent.policy import AgentPolicyHints
from pleiad.contracts import CapabilityGrants
from pleiad.runtime.limits import RunLimits


class HostPolicy:
    """Process-owner limits that no individual Run may exceed."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 600.0,
        max_model_steps: int = 32,
        max_output_tokens: int = 8_192,
        max_tool_concurrency: int = 8,
        grants: CapabilityGrants | None = None,
    ) -> None:
        """Create host limits and granted capabilities."""
        if timeout_seconds <= 0 or max_model_steps <= 0 or max_output_tokens <= 0 or max_tool_concurrency <= 0:
            raise ValueError("Host policy limits must be positive")
        self.timeout_seconds = timeout_seconds
        self.max_model_steps = max_model_steps
        self.max_output_tokens = max_output_tokens
        self.max_tool_concurrency = max_tool_concurrency
        self.grants = CapabilityGrants() if grants is None else grants


@dataclass(frozen=True, slots=True)
class EffectivePolicy:
    """Least-privileged intersection of host, Run, Agent, and parent limits."""

    timeout_seconds: float
    max_model_steps: int
    max_output_tokens: int
    tool_concurrency: int
    grants: CapabilityGrants
    deadline: datetime


def effective_policy(
    limits: RunLimits,
    host: HostPolicy,
    *,
    agent_hints: AgentPolicyHints | None = None,
    parent: EffectivePolicy | None = None,
    now: datetime | None = None,
) -> EffectivePolicy:
    """Intersect limits and permissions without allowing a child to expand them."""
    timeout = min(host.timeout_seconds, limits.execution_timeout_seconds)
    max_model_steps = min(host.max_model_steps, limits.max_model_steps)
    max_output_tokens = min(host.max_output_tokens, limits.max_output_tokens)
    tool_concurrency = min(host.max_tool_concurrency, limits.tool_concurrency)
    grants = host.grants
    if limits.grants is not None:
        grants = grants.intersect(limits.grants)
    if agent_hints is not None:
        if agent_hints.timeout_seconds is not None:
            timeout = min(timeout, agent_hints.timeout_seconds)
        if agent_hints.max_model_steps is not None:
            max_model_steps = min(max_model_steps, agent_hints.max_model_steps)
        if agent_hints.max_output_tokens is not None:
            max_output_tokens = min(max_output_tokens, agent_hints.max_output_tokens)
        if agent_hints.grants is not None:
            grants = grants.intersect(agent_hints.grants)
    current_time = now or datetime.now(UTC)
    deadline = current_time.timestamp() + timeout
    if limits.deadline is not None:
        deadline = min(deadline, limits.deadline.timestamp())
    if parent is not None:
        deadline = min(deadline, parent.deadline.timestamp())
        max_model_steps = min(max_model_steps, parent.max_model_steps)
        max_output_tokens = min(max_output_tokens, parent.max_output_tokens)
        tool_concurrency = min(tool_concurrency, parent.tool_concurrency)
        grants = grants.intersect(parent.grants)
    effective_deadline = datetime.fromtimestamp(deadline, UTC)
    return EffectivePolicy(
        timeout_seconds=max(0.0, (effective_deadline - current_time).total_seconds()),
        max_model_steps=max_model_steps,
        max_output_tokens=max_output_tokens,
        tool_concurrency=tool_concurrency,
        grants=grants,
        deadline=effective_deadline,
    )
