from dataclasses import dataclass

from pleiad.agent.agent import Agent
from pleiad.orchestration.errors import OrchestrationConfigurationError
from pleiad.runtime.execution.control import RuntimeHandoffBinding
from pleiad.runtime.runner import Runner


@dataclass(frozen=True, slots=True)
class Handoff:
    """Declare one explicit source-to-target control transition."""

    source: Agent
    target: Agent
    name: str
    description: str

    def __post_init__(self) -> None:
        """Validate the model-visible control identity."""
        if not self.name.strip():
            raise OrchestrationConfigurationError("Handoff name must not be blank.")
        if not self.description.strip():
            raise OrchestrationConfigurationError("Handoff description must not be blank.")
        if self.source.name == self.target.name:
            raise OrchestrationConfigurationError("Handoff source and target must be different Agents.")

    def bind(self, runner: Runner) -> RuntimeHandoffBinding:
        """Resolve the target's real Runtime bindings through the supplied Runner."""
        return runner.bind_handoff(
            self.source,
            self.target,
            name=self.name,
            description=self.description,
        )
