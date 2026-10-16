from dataclasses import dataclass, field

from jsonschema import SchemaError

from pleiad.agent.errors import AgentConfigurationError
from pleiad.agent.policy import AgentPolicyHints
from pleiad.contracts import JsonSchema, SkillRef, ToolRef, freeze_json_object
from pleiad.models.anthropic.client import AnthropicModelClient
from pleiad.models.openai.client import OpenAIModelClient
from pleiad.schemas import validate_json_schema
from pleiad.tools.contracts import DEFAULT_TOOL_CODES, default_tool_refs

SUPPORTED_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"


@dataclass(frozen=True, slots=True)
class Agent:
    """Reusable Agent recipe containing only immutable declarations and refs."""

    name: str
    model: OpenAIModelClient | AnthropicModelClient
    prompt: str
    tools: tuple[ToolRef, ...] = ()
    skills: tuple[SkillRef, ...] = ()
    output_schema: JsonSchema | None = None
    policy_hints: AgentPolicyHints = field(default_factory=AgentPolicyHints)

    def __post_init__(self) -> None:
        """Validate the identity, schema, and capability references."""
        if not self.name.strip():
            raise AgentConfigurationError("Agent name must not be blank.")
        if len(self.name) > 256:
            raise AgentConfigurationError("Agent name must not exceed 256 characters.")
        if not isinstance(self.model, OpenAIModelClient | AnthropicModelClient):
            raise AgentConfigurationError("Agent model must be an OpenAIModelClient or AnthropicModelClient.")
        if not self.prompt.strip():
            raise AgentConfigurationError("Agent prompt must not be blank.")
        explicit_tool_codes = tuple(ref.code for ref in self.tools)
        if len(explicit_tool_codes) != len(set(explicit_tool_codes)):
            raise AgentConfigurationError("Agent Tool refs must be unique.")
        skill_codes = tuple(ref.code for ref in self.skills)
        if len(skill_codes) != len(set(skill_codes)):
            raise AgentConfigurationError("Agent Skill refs must be unique.")
        if self.output_schema is not None:
            validate_output_schema(self.output_schema)
            object.__setattr__(self, "output_schema", freeze_json_object(self.output_schema))
        default_codes = frozenset(DEFAULT_TOOL_CODES)
        merged_tools = default_tool_refs() + tuple(ref for ref in self.tools if ref.code not in default_codes)
        object.__setattr__(self, "tools", merged_tools)


def validate_output_schema(schema: JsonSchema) -> None:
    """Validate one local Draft 2020-12 JSON Schema without network resolution."""
    dialect = schema.get("$schema")
    if dialect is not None and dialect != SUPPORTED_SCHEMA_DIALECT:
        raise AgentConfigurationError("Agent output_schema must use the Draft 2020-12 JSON Schema dialect.")
    try:
        validate_json_schema(schema)
    except ValueError as error:
        raise AgentConfigurationError(str(error)) from error
    except SchemaError as error:
        raise AgentConfigurationError("Agent output_schema is invalid.") from error
