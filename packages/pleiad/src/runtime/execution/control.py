from dataclasses import dataclass

from pleiad.agent.agent import Agent
from pleiad.runtime.policy import EffectivePolicy
from pleiad.skills.contracts import Skill
from pleiad.tools.base import BaseTool


@dataclass(frozen=True, slots=True)
class RuntimeAgentBinding:
    """Resolved Agent capabilities that Runtime can activate within one Run."""

    agent: Agent
    tools: tuple[BaseTool, ...]
    skills: tuple[Skill, ...]
    fingerprint: str


@dataclass(frozen=True, slots=True)
class RuntimeHandoffBinding:
    """Validated control binding for one source-to-target Agent transition."""

    source_agent_name: str
    target: RuntimeAgentBinding
    name: str
    description: str
    policy: EffectivePolicy | None = None

    def with_policy(self, policy: EffectivePolicy) -> "RuntimeHandoffBinding":
        """Return this binding with its Run-scoped target policy attached."""
        return RuntimeHandoffBinding(
            source_agent_name=self.source_agent_name,
            target=self.target,
            name=self.name,
            description=self.description,
            policy=policy,
        )
