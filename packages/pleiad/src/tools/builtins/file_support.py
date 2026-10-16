import hashlib
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from pleiad.tools.errors import (
    InvalidToolArgumentsError,
    ToolFileAccessDeniedError,
    ToolFileConflictError,
    ToolFileNotFoundError,
    ToolFileTooLargeError,
)


@dataclass(frozen=True, slots=True)
class FileToolBinding:
    """Host-owned roots and limits shared by the file Tools."""

    workspace: Path
    scratch: Path
    skill_roots: Mapping[str, Path] = MappingProxyType({})
    max_read_bytes: int = 1_048_576
    max_write_bytes: int = 1_048_576
    max_lines: int = 2_000

    def __post_init__(self) -> None:
        """Require existing absolute roots and positive bounded limits."""
        workspace = validate_root(self.workspace, "workspace")
        scratch = validate_root(self.scratch, "scratch")
        if self.max_read_bytes <= 0 or self.max_write_bytes <= 0 or self.max_lines <= 0:
            raise ValueError("file Tool limits must be positive")
        object.__setattr__(self, "workspace", workspace)
        object.__setattr__(self, "scratch", scratch)

        roots: dict[str, Path] = {}
        for code, root in self.skill_roots.items():
            if not code or "/" in code or "\\" in code or code in {".", ".."}:
                raise ValueError("skill root codes must be non-empty single path segments")
            roots[code] = validate_root(root, f"skill root {code}")
        object.__setattr__(self, "skill_roots", MappingProxyType(roots))


def validate_root(root: Path, name: str) -> Path:
    """Validate and resolve a host-owned file Tool root."""
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise ValueError(f"{name} root must be an existing absolute directory")
    return root.resolve()


def normalize_relative_path(value: str, scope: str) -> str:
    """Validate a model-facing relative POSIX path without exposing host paths."""
    if not value or "\x00" in value or "\\" in value:
        raise ToolFileAccessDeniedError(f"The {scope} path is invalid or empty.")
    if value.startswith("/"):
        raise ToolFileAccessDeniedError(f"The {scope} path must be relative: {value}.")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ToolFileAccessDeniedError(f"The {scope} path contains a forbidden segment: {value}.")
    return value


def resolve_read_path(
    binding: FileToolBinding,
    scope: str,
    relative_path: str,
    skill_code: str | None,
) -> Path:
    """Resolve one read scope through its owner boundary."""
    if scope == "workspace":
        return resolve_scoped_path(binding.workspace, relative_path, scope)
    if scope == "scratch":
        return resolve_scoped_path(binding.scratch, relative_path, scope)
    if skill_code is None:
        raise InvalidToolArgumentsError("skill_code is required for the skill scope.")
    try:
        root = binding.skill_roots[skill_code]
    except KeyError as error:
        raise ToolFileNotFoundError(f"The selected Skill is not available: {skill_code}.") from error
    return resolve_scoped_path(root, relative_path, scope)


def resolve_scoped_path(root: Path, relative_path: str, scope: str) -> Path:
    """Resolve a workspace or scratch path without following symlinks."""
    candidate = root.joinpath(*relative_path.split("/"))
    current = root
    for part in relative_path.split("/"):
        current = current / part
        if current.is_symlink():
            raise ToolFileAccessDeniedError(f"The {scope} path crosses a symbolic link: {relative_path}.")
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise ToolFileAccessDeniedError(f"The {scope} path is outside its permitted root: {relative_path}.") from error
    return resolved


def read_scoped_bytes(path: Path, *, scope: str, relative_path: str, max_bytes: int) -> bytes:
    """Read regular bounded bytes and map filesystem failures to Tool errors."""
    try:
        if not path.exists():
            raise ToolFileNotFoundError(f"The {scope} file does not exist: {relative_path}.")
        if not path.is_file() or path.is_symlink():
            raise ToolFileAccessDeniedError(f"The {scope} path is not an accessible regular file: {relative_path}.")
        size = path.stat().st_size
        if size > max_bytes:
            raise ToolFileTooLargeError(
                f"The {scope} file exceeds the {max_bytes} byte limit: {relative_path} ({size} bytes)."
            )
        data = path.read_bytes()
    except (ToolFileAccessDeniedError, ToolFileNotFoundError, ToolFileTooLargeError):
        raise
    except PermissionError as error:
        raise ToolFileAccessDeniedError(f"Permission was denied for the {scope} file: {relative_path}.") from error
    except FileNotFoundError as error:
        raise ToolFileNotFoundError(f"The {scope} file does not exist: {relative_path}.") from error
    except OSError as error:
        raise ToolFileAccessDeniedError(f"The {scope} file could not be read: {relative_path}.") from error
    if len(data) > max_bytes:
        raise ToolFileTooLargeError(
            f"The {scope} file exceeds the {max_bytes} byte limit: {relative_path} ({len(data)} bytes)."
        )
    return data


def select_lines(text: str, start_line: int, max_lines: int) -> tuple[str, int, bool]:
    """Return one one-based line window and whether later lines remain."""
    lines = text.splitlines(keepends=True)
    start_index = start_line - 1
    selected = lines[start_index : start_index + max_lines]
    line_end = start_line + len(selected) - 1 if selected else start_line - 1
    truncated = start_index + len(selected) < len(lines)
    return "".join(selected), line_end, truncated


def ensure_parent_directory(root: Path, target: Path, relative_path: str) -> None:
    """Create missing workspace parents while refusing symlink traversal."""
    parent = target.parent
    relative_parent = parent.relative_to(root)
    current = root
    for part in relative_parent.parts:
        current = current / part
        if current.is_symlink():
            raise ToolFileAccessDeniedError(f"The workspace path crosses a symbolic link: {relative_path}.")
        try:
            current.mkdir(exist_ok=True)
        except FileExistsError as error:
            if current.is_symlink() or not current.is_dir():
                raise ToolFileAccessDeniedError(f"The workspace parent is not a directory: {relative_path}.") from error
        except OSError as error:
            raise ToolFileAccessDeniedError(
                f"The workspace parent directory could not be created: {relative_path}."
            ) from error


def write_atomic(target: Path, data: bytes, *, create: bool, relative_path: str) -> None:
    """Write bytes through a same-directory temporary file and atomic install."""
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".pleiad-write-", delete=False) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(data)
            temporary.flush()
            os.fsync(temporary.fileno())
        if create:
            os.link(temporary_path, target)
            temporary_path.unlink()
            temporary_path = None
        else:
            os.replace(temporary_path, target)
            temporary_path = None
    except FileExistsError as error:
        raise ToolFileConflictError(f"The workspace file already exists: {relative_path}.") from error
    except PermissionError as error:
        raise ToolFileAccessDeniedError(f"Permission was denied for the workspace file: {relative_path}.") from error
    except OSError as error:
        raise ToolFileAccessDeniedError(f"The workspace file could not be written: {relative_path}.") from error
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def content_digest(data: bytes) -> str:
    """Return the stable digest used by conditional workspace writes."""
    return hashlib.sha256(data).hexdigest()
