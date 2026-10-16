from pathlib import Path

from pleiad.skills.contracts import Skill, SkillDescriptor
from pleiad.skills.errors import SkillAccessError, SkillFormatError, SkillNotFoundError
from pleiad.skills.package import SkillPackage


class SkillLoader:
    """Bind explicitly selected Skill descriptors to validated packages."""

    def __init__(self) -> None:
        self.bindings: dict[str, tuple[SkillDescriptor, SkillPackage]] = {}

    def register(self, descriptor: SkillDescriptor, package: SkillPackage) -> None:
        """Bind one descriptor to one package exactly once."""
        if descriptor.code in self.bindings:
            raise ValueError(f"Skill code is already registered: {descriptor.code}")
        self.bindings[descriptor.code] = (descriptor, package)

    def load(self, code: str) -> Skill:
        """Load instructions, metadata, and the digest for one registered package."""
        descriptor, package = self.binding(code)
        try:
            return package.load(descriptor)
        except FileNotFoundError as error:
            raise SkillNotFoundError() from error
        except PermissionError as error:
            raise SkillAccessError() from error
        except (OSError, UnicodeError) as error:
            raise SkillAccessError() from error
        except ValueError as error:
            raise SkillFormatError() from error

    def resolve(self, code: str) -> Skill:
        """Resolve a Skill snapshot without loading its instructions body."""
        descriptor, package = self.binding(code)
        try:
            return Skill(
                descriptor=descriptor,
                references=package.reference_paths(),
                content_digest=package.content_digest(),
            )
        except FileNotFoundError as error:
            raise SkillNotFoundError() from error
        except PermissionError as error:
            raise SkillAccessError() from error
        except (OSError, UnicodeError) as error:
            raise SkillAccessError() from error
        except ValueError as error:
            raise SkillFormatError() from error

    def read_file(self, code: str, relative_path: str) -> bytes:
        """Read one file from a registered Skill package on explicit request."""
        _, package = self.binding(code)
        try:
            return package.read_bytes(relative_path)
        except FileNotFoundError as error:
            raise SkillNotFoundError(f"The selected Skill file is not available: {relative_path}.") from error
        except PermissionError as error:
            raise SkillAccessError() from error
        except OSError as error:
            raise SkillAccessError() from error
        except ValueError as error:
            raise SkillFormatError() from error

    def resolve_path(self, code: str, relative_path: str) -> Path:
        """Resolve one file path from a registered Skill package."""
        _, package = self.binding(code)
        try:
            return package.resolve_path(relative_path)
        except ValueError as error:
            raise SkillFormatError() from error

    def package_root(self, code: str) -> Path:
        """Return the host-side root for one registered package."""
        _, package = self.binding(code)
        return package.root

    def binding(self, code: str) -> tuple[SkillDescriptor, SkillPackage]:
        """Return the explicit binding for a Skill code."""
        try:
            return self.bindings[code]
        except KeyError as error:
            raise SkillNotFoundError(f"No Skill package is registered for code: {code}.") from error

    def contains(self, code: str) -> bool:
        """Return whether a Skill package is registered for a code."""
        return code in self.bindings
