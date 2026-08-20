"""Static prompt assets available to generated Agents."""

from pydantic import Field

from pleiad.contracts import ContractModel, JsonObject


class SkillDescriptor(ContractModel):
    """The only Skill metadata written in the capability YAML."""

    name: str = Field(min_length=1, max_length=128)
    code: str = Field(min_length=1, max_length=256)
    description: str = Field(min_length=1, max_length=16_000)


class Skill(ContractModel):
    """Loaded static prompt content; it cannot execute or hold live config."""

    descriptor: SkillDescriptor
    instructions: str = Field(min_length=1)
    references: tuple[str, ...] = ()
    metadata: JsonObject = Field(default_factory=dict)
