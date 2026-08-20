"""Agent definition, run context, and ReAct result contracts."""

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from pleiad.errors import ErrorInfo
from pleiad.llm.client import LLMClient
from pleiad.llm.contracts import JsonObject, Message
from pleiad.llm.response.envelope import LLMResponse
from pleiad.tools.base import BaseTool
from pleiad.tools.contracts import ToolResult


class AgentStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class AgentPolicy:
    """Runtime limits applied to one Agent Run."""

    timeout_seconds: float = 600.0
    max_output_tokens: int = 8_192


@dataclass(frozen=True, slots=True)
class AgentDefinition:
    """Reusable Agent recipe containing only Model, Prompt, and Tools."""

    agent_id: str
    model_role: str
    client: LLMClient
    prompt: str
    tools: tuple[BaseTool, ...] = ()
    output_schema: JsonObject | None = None
    policy: AgentPolicy = field(default_factory=AgentPolicy)
    permissions: frozenset[str] = frozenset()
    dynamic_tool_codes: tuple[str, ...] = ()
    dynamic_skill_codes: tuple[str, ...] = ()
    tool_snapshot_id: str | None = None
    skill_snapshot_id: str | None = None


@dataclass(frozen=True, slots=True)
class AgentContext:
    """Ephemeral context belonging to one Agent Run."""

    messages: tuple[Message, ...] = ()
    metadata: JsonObject = field(default_factory=dict)
    conversation_id: str = "default-conversation"
    raw_query: str | None = None
    checkpoint_handoff: str | None = None
    history_sequence: int = 0
    checkpoint_sink: Callable[[object], None] | None = None


@dataclass(frozen=True, slots=True)
class AgentResult:
    """Final result of one ReAct loop."""

    run_id: str
    status: AgentStatus
    text: str = ""
    structured_output: object | None = None
    response: LLMResponse | None = None
    tool_results: tuple[ToolResult, ...] = ()
    error: ErrorInfo | None = None
