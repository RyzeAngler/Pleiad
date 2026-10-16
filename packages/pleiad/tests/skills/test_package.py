from pathlib import Path

import pytest

from pleiad.skills.contracts import SkillDescriptor
from pleiad.skills.package import SkillPackage


def descriptor(code: str = "demo") -> SkillDescriptor:
    """Build a descriptor matching a package fixture."""
    return SkillDescriptor(name=code, code=code, description=f"{code} skill")


def create_package(root: Path, body: str = "Use this skill.") -> SkillPackage:
    """Create a valid package with one UTF-8 reference."""
    root.mkdir()
    (root / "SKILL.md").write_text(
        f"---\nname: {root.name}\ndescription: {root.name} skill\n---\n{body}\n",
        encoding="utf-8",
    )
    (root / "references").mkdir()
    (root / "references" / "guide.md").write_text("reference text", encoding="utf-8")
    return SkillPackage(root)


def test_skill_package_load_returns_instructions_inventory_and_digest(tmp_path: Path) -> None:
    """Loading a package returns its instructions and content identity."""
    package = create_package(tmp_path / "demo")

    skill = package.load(descriptor())

    assert skill.descriptor.code == "demo"
    assert skill.instructions == "Use this skill."
    assert skill.references == ("references/guide.md",)
    assert len(skill.content_digest) == 64


def test_skill_package_reads_a_reference_only_when_explicitly_requested(tmp_path: Path) -> None:
    """Reference bytes are available through the explicit package file boundary."""
    package = create_package(tmp_path / "demo")

    assert package.read_bytes("references/guide.md") == b"reference text"


def test_skill_package_rejects_reference_path_escape(tmp_path: Path) -> None:
    """A reference cannot address a file outside the package root."""
    package = create_package(tmp_path / "demo")

    with pytest.raises(ValueError, match="package paths"):
        package.read_bytes("../outside.md")


def test_skill_package_rejects_symlinked_reference(tmp_path: Path) -> None:
    """A Skill package cannot use a symlink to bypass its resource boundary."""
    package_root = tmp_path / "demo"
    package = create_package(package_root)
    outside = tmp_path / "outside.md"
    outside.write_text("private", encoding="utf-8")
    (package.root / "references" / "escape.md").symlink_to(outside)

    with pytest.raises(ValueError, match="symlink"):
        SkillPackage(package_root)


def test_skill_package_digest_changes_when_a_reference_changes(tmp_path: Path) -> None:
    """Changing package content produces a different activation identity."""
    package = create_package(tmp_path / "demo")
    before = package.content_digest()
    (package.root / "references" / "guide.md").write_text("changed", encoding="utf-8")

    assert package.content_digest() != before
