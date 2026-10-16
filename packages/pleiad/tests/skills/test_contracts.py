import pytest
from pydantic import ValidationError

from pleiad.skills.contracts import Skill, SkillDescriptor


def test_skill_descriptor_preserves_standard_frontmatter_metadata() -> None:
    """A descriptor exposes identity and optional standard metadata to the host."""
    descriptor = SkillDescriptor(
        name="demo",
        code="demo",
        description="A demo skill.",
        license="Apache-2.0",
        compatibility="Python 3.12",
        metadata={"owner": "sdk"},
        allowed_tools=("read_file", "command"),
    )

    assert descriptor.name == "demo"
    assert descriptor.allowed_tools == ("read_file", "command")
    assert descriptor.metadata == {"owner": "sdk"}


def test_skill_descriptor_rejects_codes_that_are_not_safe_slugs() -> None:
    """A Skill code cannot become an ambiguous virtual path segment."""
    with pytest.raises(ValidationError):
        SkillDescriptor(name="demo", code="pleiad.skill.demo", description="A demo skill.")


def test_loaded_skill_requires_a_content_digest() -> None:
    """An activated Skill carries an immutable package content identity."""
    skill = Skill(
        descriptor=SkillDescriptor(name="demo", code="demo", description="A demo skill."),
        instructions="Use this skill.",
        references=("references/guide.md",),
        content_digest="0" * 64,
    )

    assert skill.content_digest == "0" * 64
