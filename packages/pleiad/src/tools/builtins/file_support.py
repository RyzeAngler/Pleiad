import asyncio
import fcntl
import hashlib
import os
import stat
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from uuid import uuid4

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
    excluded_workspace_paths: tuple[str, ...] = ()
    workspace_mask_root: Path | None = None

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
        exclusions: list[str] = []
        for value in self.excluded_workspace_paths:
            try:
                relative_path = normalize_relative_path(value, "excluded workspace")
            except ToolFileAccessDeniedError as error:
                raise ValueError("Excluded workspace paths must be relative POSIX directory paths") from error
            if any(
                relative_path == previous
                or relative_path.startswith(previous + "/")
                or previous.startswith(relative_path + "/")
                for previous in exclusions
            ):
                raise ValueError("Excluded workspace paths must not overlap")
            exclusions.append(relative_path)
        object.__setattr__(self, "excluded_workspace_paths", tuple(sorted(exclusions)))
        if self.workspace_mask_root is not None:
            mask_root = validate_root(self.workspace_mask_root, "workspace mask")
            if any(
                mask_root.is_relative_to(root) or root.is_relative_to(mask_root)
                for root in (workspace, scratch, *roots.values())
            ):
                raise ValueError("The workspace mask must be outside every exposed file Tool root")
            if any(mask_root.iterdir()):
                raise ValueError("The workspace mask must be an empty host-owned directory")
            object.__setattr__(self, "workspace_mask_root", mask_root)

    def validate_workspace_path(self, relative_path: str) -> None:
        """Reject access to a normalized path inside a host-excluded subtree.

        Args:
            relative_path: A path already checked by normalize_relative_path.

        Raises:
            ToolFileAccessDeniedError: If the path names an excluded subtree.
        """
        if any(
            relative_path == excluded or relative_path.startswith(excluded + "/")
            for excluded in self.excluded_workspace_paths
        ):
            raise ToolFileAccessDeniedError("The workspace path belongs to a host-owned excluded directory.")


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


def read_scope_root(
    binding: FileToolBinding,
    scope: str,
    skill_code: str | None,
) -> Path:
    """Select a host-owned root without resolving model-controlled paths."""
    if scope == "workspace":
        return binding.workspace
    if scope == "scratch":
        return binding.scratch
    if skill_code is None:
        raise InvalidToolArgumentsError("skill_code is required for the skill scope.")
    try:
        root = binding.skill_roots[skill_code]
    except KeyError as error:
        raise ToolFileNotFoundError(f"The selected Skill is not available: {skill_code}.") from error
    return root


@contextmanager
def scoped_parent(root: Path, relative_path: str, *, create: bool = False) -> Iterator[tuple[int, str]]:
    """Pin every directory component without following replacement symlinks."""
    parts = normalize_relative_path(relative_path, "file").split("/")
    directory = os.open(root.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in root.parts[1:]:
            child = open_directory(directory, part, create=False)
            os.close(directory)
            directory = child
        for part in parts[:-1]:
            child = open_directory(directory, part, create=create)
            os.close(directory)
            directory = child
        yield directory, parts[-1]
    finally:
        os.close(directory)


def open_directory(parent: int, name: str, *, create: bool) -> int:
    """Open or create one directory relative to a pinned parent descriptor."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        return os.open(name, flags, dir_fd=parent)
    except FileNotFoundError:
        if not create:
            raise
        try:
            os.mkdir(name, dir_fd=parent)
        except FileExistsError:
            return os.open(name, flags, dir_fd=parent)
        return os.open(name, flags, dir_fd=parent)


def read_regular_bytes(parent: int, name: str, *, scope: str, relative_path: str, max_bytes: int) -> bytes:
    """Read at most one excess byte from a regular, non-symlink file."""
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    with os.fdopen(descriptor, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode):
            raise ToolFileAccessDeniedError(f"The {scope} path is not an accessible regular file: {relative_path}.")
        if metadata.st_size > max_bytes:
            raise ToolFileTooLargeError(f"The {scope} file exceeds the {max_bytes} byte limit: {relative_path}.")
        data = stream.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ToolFileTooLargeError(f"The {scope} file exceeds the {max_bytes} byte limit: {relative_path}.")
    return data


def read_scoped_bytes(root: Path, *, scope: str, relative_path: str, max_bytes: int) -> bytes:
    """Read through pinned directories and translate filesystem failures."""
    try:
        with scoped_parent(root, relative_path) as (parent, name):
            return read_regular_bytes(parent, name, scope=scope, relative_path=relative_path, max_bytes=max_bytes)
    except FileNotFoundError as error:
        raise ToolFileNotFoundError(f"The {scope} file does not exist: {relative_path}.") from error
    except OSError as error:
        raise ToolFileAccessDeniedError(
            f"The {scope} path is not an accessible regular file: {relative_path}."
        ) from error


def select_lines(text: str, start_line: int, max_lines: int) -> tuple[str, int, bool]:
    """Return one one-based line window and whether later lines remain."""
    lines = text.splitlines(keepends=True)
    start_index = start_line - 1
    selected = lines[start_index : start_index + max_lines]
    line_end = start_line + len(selected) - 1 if selected else start_line - 1
    truncated = start_index + len(selected) < len(lines)
    return "".join(selected), line_end, truncated


@contextmanager
def directory_write_lock(parent: int, relative_path: str) -> Iterator[None]:
    """Serialize cooperating SDK writers without creating model-visible lock files."""
    deadline = time.monotonic() + 10.0
    while True:
        try:
            fcntl.flock(parent, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError as error:
            if time.monotonic() >= deadline:
                raise ToolFileConflictError(f"The workspace directory is busy: {relative_path}.") from error
            time.sleep(0.01)
    try:
        yield
    finally:
        fcntl.flock(parent, fcntl.LOCK_UN)


def write_scoped_bytes(
    root: Path,
    relative_path: str,
    data: bytes,
    *,
    expected_sha256: str | None,
    max_bytes: int,
) -> None:
    """Create or replace text under a directory lock shared by SDK processes.

    The digest is checked after staging and immediately before installation.
    Writers that ignore the advisory lock can still race the final check and
    replacement; this operation is not an atomic CAS against arbitrary editors.
    """
    try:
        with (
            scoped_parent(root, relative_path, create=True) as (parent, name),
            directory_write_lock(parent, relative_path),
        ):
            temporary_name = f".pleiad-write-{uuid4().hex}"
            descriptor = os.open(
                temporary_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent
            )
            staged = True
            try:
                with os.fdopen(descriptor, "wb") as temporary:
                    temporary.write(data)
                    temporary.flush()
                    os.fsync(temporary.fileno())
                if expected_sha256 is None:
                    try:
                        read_regular_bytes(
                            parent, name, scope="workspace", relative_path=relative_path, max_bytes=max_bytes
                        )
                    except FileNotFoundError:
                        os.link(temporary_name, name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
                    else:
                        raise ToolFileConflictError(f"The workspace file already exists: {relative_path}.")
                else:
                    current = read_regular_bytes(
                        parent, name, scope="workspace", relative_path=relative_path, max_bytes=max_bytes
                    )
                    if content_digest(current) != expected_sha256:
                        raise ToolFileConflictError(f"The workspace file changed before replacement: {relative_path}.")
                    os.replace(temporary_name, name, src_dir_fd=parent, dst_dir_fd=parent)
                    staged = False
            finally:
                if staged:
                    os.unlink(temporary_name, dir_fd=parent)
    except FileExistsError as error:
        raise ToolFileConflictError(f"The workspace file already exists: {relative_path}.") from error
    except FileNotFoundError as error:
        raise ToolFileNotFoundError(f"The workspace file does not exist: {relative_path}.") from error
    except OSError as error:
        raise ToolFileAccessDeniedError(f"The workspace file could not be written: {relative_path}.") from error


async def run_file_io[ResultT](operation: Callable[[], ResultT]) -> ResultT:
    """Finish owned filesystem cleanup before propagating cancellation."""
    task = asyncio.create_task(asyncio.to_thread(operation))
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancelled = True
            continue
        except Exception as error:
            if cancelled:
                raise asyncio.CancelledError from error
            raise
        if cancelled:
            raise asyncio.CancelledError
        return result


def content_digest(data: bytes) -> str:
    """Return the stable digest used by conditional workspace writes."""
    return hashlib.sha256(data).hexdigest()
