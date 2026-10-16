import hashlib
import stat
from dataclasses import dataclass
from pathlib import Path

from pleiad.skills.contracts import Skill, SkillDescriptor
from pleiad.skills.manifest import extract_skill_instructions, load_skill_descriptor


@dataclass(frozen=True, slots=True)
class SkillPackage:
    """A validated Skill root containing SKILL.md and optional resources."""

    root: Path

    def __post_init__(self) -> None:
        """Require an absolute, regular package root with SKILL.md."""
        if not self.root.is_absolute():
            raise ValueError("Skill package root must be absolute")
        if self.root.is_symlink():
            raise ValueError("Skill package root must not be a symlink")
        resolved_root = self.root.resolve()
        if not resolved_root.is_dir():
            raise ValueError(f"Skill package root does not exist: {resolved_root}")
        if not (resolved_root / "SKILL.md").is_file():
            raise ValueError(f"Skill package is missing SKILL.md: {resolved_root}")
        object.__setattr__(self, "root", resolved_root)
        self.validate_tree()

    @property
    def instructions_path(self) -> Path:
        """Return the conventional package instructions file."""
        return self.root / "SKILL.md"

    def load(self, descriptor: SkillDescriptor) -> Skill:
        """Validate the selected descriptor and return its loaded package."""
        instructions_content = self.instructions_path.read_text(encoding="utf-8")
        package_descriptor = load_skill_descriptor(self.instructions_path)
        if descriptor.code != package_descriptor.code:
            raise ValueError("Selected Skill code does not match the package directory")
        if descriptor.name != package_descriptor.name or descriptor.description != package_descriptor.description:
            raise ValueError("Selected Skill metadata does not match SKILL.md frontmatter")
        return Skill(
            descriptor=descriptor,
            instructions=extract_skill_instructions(instructions_content),
            references=self.reference_paths(),
            content_digest=self.content_digest(),
        )

    def read_bytes(self, relative_path: str) -> bytes:
        """Read one package-relative file after validating its path."""
        resolved = self.resolve_path(relative_path)
        if not resolved.is_file():
            raise FileNotFoundError(f"Skill file does not exist: {relative_path}")
        return resolved.read_bytes()

    def resolve_path(self, relative_path: str) -> Path:
        """Resolve a normalized relative POSIX package path safely."""
        if not relative_path or "\x00" in relative_path or "\\" in relative_path:
            raise ValueError("Skill package paths must be non-empty normalized POSIX paths")
        if relative_path.startswith("/"):
            raise ValueError("Skill package paths must remain below the package root")
        parts = relative_path.split("/")
        if any(part in {"", ".", ".."} for part in parts):
            raise ValueError("Skill package paths must be non-empty normalized POSIX paths")
        candidate = self.root.joinpath(*parts)
        current = self.root
        for part in parts:
            current = current / part
            if current.is_symlink():
                raise ValueError("Skill package paths must not traverse symlinks")
        resolved = candidate.resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError as error:
            raise ValueError("Skill package paths must remain below the package root") from error
        return resolved

    def reference_paths(self) -> tuple[str, ...]:
        """Return deterministic relative paths for regular reference files."""
        references_root = self.root / "references"
        if not references_root.is_dir():
            return ()
        paths: list[str] = []
        for candidate in sorted(references_root.rglob("*"), key=lambda path: path.as_posix()):
            relative = candidate.relative_to(self.root).as_posix()
            resolved = self.resolve_path(relative)
            if resolved.is_file() and stat.S_ISREG(resolved.stat().st_mode):
                paths.append(relative)
        return tuple(paths)

    def content_digest(self) -> str:
        """Hash every regular package file by path and content."""
        digest = hashlib.sha256()
        files = [
            candidate
            for candidate in self.root.rglob("*")
            if candidate.is_file() and stat.S_ISREG(candidate.stat().st_mode)
        ]
        for candidate in sorted(files, key=lambda path: path.relative_to(self.root).as_posix()):
            relative = candidate.relative_to(self.root).as_posix().encode("utf-8")
            digest.update(relative)
            digest.update(b"\0")
            digest.update(candidate.read_bytes())
        return digest.hexdigest()

    def validate_tree(self) -> None:
        """Reject symlinks and non-regular entries in the package tree."""
        for candidate in self.root.rglob("*"):
            if candidate.is_symlink():
                raise ValueError("Skill packages must not contain symlinks")
            if not candidate.is_dir() and not stat.S_ISREG(candidate.stat().st_mode):
                raise ValueError("Skill packages may contain only directories and regular files")
