from pleiad.agent.prompt import build_prompt
from pleiad.skills.contracts import Skill, SkillDescriptor
from pleiad.tools.contracts import ToolDescriptor


def descriptor(code: str, description: str | None = None) -> ToolDescriptor:
    """Build a Tool descriptor for prompt projection tests."""
    return ToolDescriptor(name=code, code=code, description=description or f"{code} tool")


def skill() -> Skill:
    """Build a resolved Skill snapshot without instructions."""
    return Skill(
        descriptor=SkillDescriptor(name="demo", code="demo", description="Demo skill"),
        content_digest="0" * 64,
    )


def test_prompt_describes_only_available_builtin_tools() -> None:
    """The prompt never advertises a built-in Tool absent from the current snapshot."""
    prompt = build_prompt(
        "Base prompt",
        tools=(descriptor("read_file"), descriptor("command")),
        skills=(),
    )

    assert "read_file" in prompt
    assert "command" in prompt
    assert "write_file" not in prompt


def test_skill_catalog_does_not_load_instructions() -> None:
    """The initial Skill prompt contains metadata but not its private body."""
    prompt = build_prompt("Base prompt", tools=(descriptor("read_file"),), skills=(skill(),))

    assert "`demo`: Demo skill" in prompt
    assert "SKILL.md" in prompt
    assert "instructions body" not in prompt
