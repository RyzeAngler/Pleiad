"""Lazy Skill implementation loading by opaque code."""

from collections.abc import Callable

from pleiad.skills.contracts import Skill, SkillDescriptor

SkillFactory = Callable[[SkillDescriptor], Skill]


class SkillLoader:
    """Resolve a registered Skill code without exposing implementation paths."""

    def __init__(self) -> None:
        self._factories: dict[str, SkillFactory] = {}

    def register(self, code: str, factory: SkillFactory) -> None:
        """Register one Skill resolver exactly once."""
        if code in self._factories:
            raise ValueError(f"Skill code is already registered: {code}")
        self._factories[code] = factory

    def load(self, descriptor: SkillDescriptor) -> Skill:
        """Load one selected Skill."""
        try:
            skill = self._factories[descriptor.code](descriptor)
        except KeyError as exc:
            raise LookupError(f"No Skill loader registered for code: {descriptor.code}") from exc
        if skill.descriptor.code != descriptor.code:
            raise ValueError(
                f"Skill loader returned code {skill.descriptor.code!r} for {descriptor.code!r}"
            )
        return skill

    def contains(self, code: str) -> bool:
        """Return whether a resolver exists for a Skill code."""
        return code in self._factories
