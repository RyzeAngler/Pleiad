from pleiad.errors.base import SystemFault


class SkillNotFoundError(SystemFault):
    """Report a selected Skill package or file that does not exist."""

    def __init__(self, message: str = "The selected Skill is not available.") -> None:
        """Create a missing Skill fault."""
        super().__init__("skill.not_found", message)


class SkillFormatError(SystemFault):
    """Report invalid Skill metadata, instructions, or package structure."""

    def __init__(self, message: str = "The selected Skill package is invalid.") -> None:
        """Create an invalid Skill format fault."""
        super().__init__("skill.format.invalid", message)


class SkillAccessError(SystemFault):
    """Report a filesystem failure while accessing a selected Skill."""

    def __init__(self, message: str = "The selected Skill package cannot be accessed.") -> None:
        """Create a Skill access fault."""
        super().__init__("skill.access.failed", message)
