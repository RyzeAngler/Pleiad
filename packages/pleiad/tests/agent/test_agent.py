import pytest
from pydantic import ValidationError

from pleiad.agent.agent import Agent
from pleiad.agent.errors import AgentConfigurationError
from pleiad.agent.policy import AgentPolicyHints
from pleiad.contracts import CapabilityGrants, JsonSchema, SkillRef, ToolRef, thaw_json_object
from pleiad.models.openai.client import OpenAIModelClient
from pleiad.runtime.limits import RunLimits
from pleiad.runtime.policy import HostPolicy, effective_policy


class ModelStub(OpenAIModelClient):
    """Protocol-shaped model stub that does not contact a provider."""

    def __init__(self) -> None:
        self.provider = "fake"
        self.model = "fake-model"


def agent(
    *,
    tools: tuple[ToolRef, ...] = (),
    skills: tuple[SkillRef, ...] = (),
    output_schema: JsonSchema | None = None,
    policy_hints: AgentPolicyHints | None = None,
) -> Agent:
    """Build an Agent with the smallest valid model boundary."""
    return Agent(
        name="assistant",
        model=ModelStub(),
        prompt="Answer the user.",
        tools=tools,
        skills=skills,
        output_schema=output_schema,
        policy_hints=AgentPolicyHints() if policy_hints is None else policy_hints,
    )


def test_agent_rejects_blank_name() -> None:
    """Whitespace-only Agent names are configuration faults."""
    with pytest.raises(AgentConfigurationError, match="name must not be blank"):
        Agent(name=" \t", model=ModelStub(), prompt="Answer the user.")


def test_agent_rejects_duplicate_tool_refs() -> None:
    """Repeated explicit Tool references fail before execution."""
    with pytest.raises(AgentConfigurationError, match="Tool refs must be unique"):
        agent(tools=(ToolRef("custom"), ToolRef("custom")))


def test_agent_installs_default_tools_when_selection_is_omitted() -> None:
    configured = Agent(name="default", model=ModelStub(), prompt="Answer the user.")

    assert tuple(ref.code for ref in configured.tools) == ("read_file", "write_file", "command")


@pytest.mark.parametrize("codes", [(), ("read_file",), ("write_file", "custom")])
def test_explicit_agent_tools_are_the_exact_host_selected_set(codes: tuple[str, ...]) -> None:
    configured = agent(tools=tuple(ToolRef(code) for code in codes))

    assert tuple(ref.code for ref in configured.tools) == codes


def test_empty_agent_grants_do_not_enable_tools() -> None:
    """An explicitly empty Agent grant set remains empty after policy intersection."""
    hints = AgentPolicyHints(grants=CapabilityGrants(capabilities=frozenset()))
    policy = effective_policy(
        RunLimits(grants=CapabilityGrants(capabilities=frozenset({"file.read"}))),
        HostPolicy(grants=CapabilityGrants(capabilities=frozenset({"file.read"}))),
        agent_hints=hints,
    )

    assert policy.grants.capabilities == frozenset()


def test_policy_hints_preserve_none_grants() -> None:
    """Omitted Agent grants remain distinct from an explicitly empty grant set."""
    assert AgentPolicyHints().grants is None
    assert AgentPolicyHints(grants=CapabilityGrants(capabilities=frozenset())).grants == CapabilityGrants(
        capabilities=frozenset()
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("timeout_seconds", 0.0),
        ("timeout_seconds", float("inf")),
        ("max_model_steps", 0),
        ("max_output_tokens", 0),
    ],
)
def test_policy_hints_reject_invalid_bounds(field: str, value: float | int) -> None:
    """Policy bounds must be positive, and timeout must be finite."""
    with pytest.raises(ValidationError):
        AgentPolicyHints.model_validate({field: value})


def test_agent_rejects_invalid_output_schema() -> None:
    """An invalid JSON Schema is a system configuration fault."""
    with pytest.raises(AgentConfigurationError, match="output_schema is invalid"):
        agent(output_schema={"type": "not-a-json-schema-type"})


def test_agent_output_schema_isolated_from_caller_mutation() -> None:
    """Mutating the caller's schema does not change the Agent contract."""
    answer_schema: JsonSchema = {"type": "string"}
    schema: JsonSchema = {"type": "object", "properties": {"answer": answer_schema}}
    configured = agent(output_schema=schema)

    answer_schema["type"] = "integer"

    assert thaw_json_object(configured.output_schema or {})["properties"] == {"answer": {"type": "string"}}


def test_agent_rejects_remote_output_schema_reference() -> None:
    """An output Schema cannot trigger implicit remote resolution."""
    with pytest.raises(AgentConfigurationError, match="remote references"):
        agent(output_schema={"$ref": "https://example.com/schema.json"})


def test_agent_rejects_duplicate_skill_refs() -> None:
    """Repeated Skill references fail as configuration faults."""
    with pytest.raises(AgentConfigurationError, match="Skill refs must be unique"):
        agent(skills=(SkillRef("demo"), SkillRef("demo")))
