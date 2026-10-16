import re

from pydantic import ConfigDict, Field

from pleiad.contracts import ContractModel

SKILL_SLUG_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"


class SkillDescriptor(ContractModel):
    """The metadata exposed to an agent for one Skill package."""

    model_config = ConfigDict(strict=True)

    name: str = Field(min_length=1, max_length=64)
    code: str = Field(min_length=1, max_length=64, pattern=SKILL_SLUG_PATTERN)
    description: str = Field(min_length=1, max_length=1_024)
    license: str | None = Field(default=None, max_length=512)
    compatibility: str | None = Field(default=None, max_length=512)
    metadata: dict[str, str] = Field(default_factory=dict)
    allowed_tools: tuple[str, ...] = ()


class Skill(ContractModel):
    """Selected Skill metadata, optional instructions, and package inventory."""

    model_config = ConfigDict(strict=True)

    descriptor: SkillDescriptor
    instructions: str | None = Field(default=None, min_length=1)
    references: tuple[str, ...] = ()
    content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


def is_skill_slug(value: str) -> bool:
    """Return whether a Skill code is a safe lowercase package slug."""
    return bool(re.fullmatch(SKILL_SLUG_PATTERN, value))
