"""Built-in static Skills and file-backed Skill factories."""

from collections.abc import Callable
from pathlib import Path

from pleiad.skills.contracts import Skill, SkillDescriptor
from pleiad.skills.zoo import SkillZoo


def file_skill_factory(root: Path) -> Callable[[SkillDescriptor], Skill]:
    """Create a loader that reads ``<root>/<name>/SKILL.md`` at selection time."""

    def load(descriptor: SkillDescriptor) -> Skill:
        path = root / descriptor.name / "SKILL.md"
        if not path.is_file():
            raise FileNotFoundError(f"Skill file not found for {descriptor.code}: {path}")
        return Skill(descriptor=descriptor, instructions=path.read_text(encoding="utf-8"))

    return load


def register_file_skills(
    zoo: SkillZoo,
    descriptors: tuple[SkillDescriptor, ...],
    root: Path,
) -> None:
    """Register workspace Skill descriptors with static file-backed loaders."""
    factory = file_skill_factory(root)
    for descriptor in descriptors:
        zoo.register(descriptor, factory)
