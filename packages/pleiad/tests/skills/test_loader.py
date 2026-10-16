from pathlib import Path

import pytest

from pleiad.skills.contracts import SkillDescriptor
from pleiad.skills.errors import SkillFormatError, SkillNotFoundError
from pleiad.skills.loader import SkillLoader
from pleiad.skills.package import SkillPackage


def package(root: Path, code: str) -> tuple[SkillDescriptor, SkillPackage]:
    """Create one descriptor and its frontmatter package."""
    root.mkdir()
    (root / "SKILL.md").write_text(
        f"---\nname: {code}\ndescription: {code} skill\n---\nbody for {code}\n",
        encoding="utf-8",
    )
    descriptor = SkillDescriptor(name=code, code=code, description=f"{code} skill")
    return descriptor, SkillPackage(root)


def test_skill_loader_loads_selected_instructions_and_reads_files_on_demand(tmp_path: Path) -> None:
    """Loading one code activates instructions while reference reads remain explicit."""
    descriptor, skill_package = package(tmp_path / "second", "second")
    (skill_package.root / "references").mkdir()
    (skill_package.root / "references" / "value.md").write_text("second", encoding="utf-8")
    loader = SkillLoader()
    loader.register(descriptor, skill_package)

    loaded = loader.load("second")

    assert loaded.descriptor.code == "second"
    assert loaded.instructions == "body for second"
    assert loader.read_file("second", "references/value.md") == b"second"


def test_skill_loader_rejects_duplicate_skill_codes(tmp_path: Path) -> None:
    """A host snapshot cannot make one Skill code resolve ambiguously."""
    first_descriptor, first_package = package(tmp_path / "first", "demo")
    second_descriptor, second_package = package(tmp_path / "second", "demo")
    loader = SkillLoader()
    loader.register(first_descriptor, first_package)

    with pytest.raises(ValueError, match="already registered"):
        loader.register(second_descriptor, second_package)


def test_skill_loader_rejects_unknown_skill_codes() -> None:
    """An unregistered code fails at the explicit activation boundary."""
    with pytest.raises(SkillNotFoundError, match="No Skill package"):
        SkillLoader().load("missing")


def test_skill_loader_classifies_missing_selected_file(tmp_path: Path) -> None:
    """A missing on-demand Skill resource has a stable execution-boundary code."""
    descriptor, skill_package = package(tmp_path / "demo", "demo")
    loader = SkillLoader()
    loader.register(descriptor, skill_package)

    with pytest.raises(SkillNotFoundError) as caught:
        loader.read_file("demo", "references/missing.md")

    assert caught.value.code == "skill.not_found"


def test_skill_loader_classifies_invalid_selected_path(tmp_path: Path) -> None:
    """An unsafe Skill resource path is distinguished from absence and access failure."""
    descriptor, skill_package = package(tmp_path / "demo", "demo")
    loader = SkillLoader()
    loader.register(descriptor, skill_package)

    with pytest.raises(SkillFormatError) as caught:
        loader.read_file("demo", "../secret")

    assert caught.value.code == "skill.format.invalid"
