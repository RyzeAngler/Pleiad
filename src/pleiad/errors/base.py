"""Central error vocabulary and loop termination policy."""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    """Stable machine-readable error codes used across Pleiad layers."""

    AGENT_MODEL = "agent.model_error"
    AGENT_OUTPUT = "agent.invalid_output"
    AGENT_CANCELLED = "agent.cancelled"
    TOOL_UNKNOWN = "tool.unknown"
    TOOL_INVALID_ARGUMENTS = "tool.invalid_arguments"
    TOOL_PERMISSION = "tool.permission_denied"
    TOOL_TIMEOUT = "tool.timeout"
    TOOL_OUTPUT = "tool.invalid_output"
    TOOL_EXECUTION = "tool.execution_error"
    GRAPH_INVALID = "graph.invalid"
    GRAPH_CONFLICT = "graph.revision_conflict"
    GRAPH_EXECUTION = "graph.execution_error"
    SYSTEM_CONFIGURATION = "system.configuration_error"
    SYSTEM_CAPABILITY = "system.capability_error"
    SYSTEM_CAPABILITY_RELOAD = "system.capability_reload_error"
    SYSTEM_STORAGE = "system.storage_error"
    SYSTEM_UNEXPECTED = "system.unexpected_error"


class ErrorKind(StrEnum):
    """Broad error ownership used by observability and recovery."""

    AGENT = "agent"
    TOOL = "tool"
    GRAPH = "graph"
    SYSTEM = "system"


@dataclass(frozen=True, slots=True)
class ErrorInfo:
    """Structured error information carried through Agent and Runtime state."""

    code: str
    message: str
    kind: ErrorKind
    retryable: bool = False
    terminal: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-compatible diagnostic projection."""
        return {
            "code": self.code,
            "message": self.message,
            "kind": self.kind.value,
            "retryable": self.retryable,
            "terminal": self.terminal,
            "details": dict(self.details),
        }


class PleiadError(Exception):
    """Base exception whose structured info decides whether a loop can continue."""

    def __init__(self, info: ErrorInfo) -> None:
        super().__init__(info.message)
        self.info = info


class AgentError(PleiadError):
    """Model and Agent-loop exception boundary."""


class ToolExecutionError(PleiadError):
    """Executable Tool exception boundary."""


class SystemError(PleiadError):
    """Process, configuration, persistence, and invariant exception boundary."""
