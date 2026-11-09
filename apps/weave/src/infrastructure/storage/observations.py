import hashlib
import os
import stat
import threading
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from time import perf_counter
from typing import Literal

from pleiad.contracts import ContractModel, JsonObject
from pydantic import AwareDatetime, Field

from pleiad_weave.infrastructure.storage.product import ProductStore, fingerprint
from pleiad_weave.infrastructure.storage.product_models import Page, ProductCapacityError, ProductConflict


class ObservedFile(ContractModel):
    """Retain bounded regular-file metadata without copying external file content."""

    relative_path: str
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    size: int = Field(ge=0)
    modified_at_ns: int


class DirectoryState(ContractModel):
    """Persist the last complete observation independently from product work."""

    source_id: str
    revision: int = Field(ge=1)
    observation_count: int = Field(ge=0)
    files: dict[str, ObservedFile]
    checked_at: AwareDatetime


class SourceObservation(ContractModel):
    """Describe a confirmed created, changed, or deleted external file."""

    observation_id: str
    source_id: str
    sequence: int = Field(ge=1)
    kind: Literal["created", "changed", "deleted"]
    relative_path: str
    previous_digest: str | None
    digest: str | None
    size: int | None
    observed_at: AwareDatetime


class ObservationReceipt(ContractModel):
    """Confirm one explicit read-only scan and its committed observation identities."""

    request_id: str
    source_id: str
    revision: int
    file_count: int
    ignored_count: int
    bytes_read: int
    observation_ids: tuple[str, ...]
    checked_at: AwareDatetime
    duration_seconds: float = Field(ge=0)
    duplicate: bool = False


class SourceHealth(ContractModel):
    """Report the real on-demand accessibility check without running a model."""

    source_id: str
    state: Literal["available", "missing", "forbidden", "invalid", "unavailable"]
    available: bool
    checked_at: AwareDatetime
    duration_seconds: float = Field(ge=0)


class DirectoryObserver:
    """Observe one host-authorized directory without dispatching or modifying work.

    Args:
        store: Product journal owning deduplication and observation history.
        directory: Explicit host configuration outside model-provided input.
        max_files: Maximum regular files in a complete scan.
        max_file_bytes: Maximum bytes read from one file.
        max_total_bytes: Maximum combined bytes read in one scan.
    """

    def __init__(
        self,
        store: ProductStore,
        directory: Path,
        *,
        max_files: int = 1000,
        max_file_bytes: int = 25 * 1024 * 1024,
        max_total_bytes: int = 100 * 1024 * 1024,
    ) -> None:
        if min(max_files, max_file_bytes, max_total_bytes) < 1:
            raise ValueError("Directory observation bounds must be positive")
        self.store = store
        self.directory = directory.expanduser().absolute()
        self.max_files = max_files
        self.max_file_bytes = max_file_bytes
        self.max_total_bytes = max_total_bytes
        self.configuration_digest = fingerprint(
            {
                "directory": str(self.directory),
                "max_files": max_files,
                "max_file_bytes": max_file_bytes,
                "max_total_bytes": max_total_bytes,
            }
        )
        self.source_id = f"directory:{hashlib.sha256(str(self.directory).encode()).hexdigest()[:24]}"
        self.lock = threading.Lock()

    def configuration(self) -> JsonObject:
        """Expose safe source scope and limits without revealing its absolute path."""
        return {
            "source_id": self.source_id,
            "kind": "directory",
            "mode": "observe",
            "configuration_location": ".env",
            "configured": True,
            "max_files": self.max_files,
            "max_file_bytes": self.max_file_bytes,
            "max_total_bytes": self.max_total_bytes,
            "follows_symlinks": False,
            "creates_executions": False,
        }

    def health(self) -> SourceHealth:
        """Check the actual configured directory only when explicitly requested."""
        started = perf_counter()
        try:
            metadata = self.directory.lstat()
            if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                state: Literal["available", "missing", "forbidden", "invalid", "unavailable"] = "invalid"
            elif os.access(self.directory, os.R_OK | os.X_OK):
                state = "available"
            else:
                state = "forbidden"
        except FileNotFoundError:
            state = "missing"
        except PermissionError:
            state = "forbidden"
        except OSError:
            state = "unavailable"
        return SourceHealth(
            source_id=self.source_id,
            state=state,
            available=state == "available",
            checked_at=datetime.now(UTC),
            duration_seconds=perf_counter() - started,
        )

    def scan_files(self) -> tuple[dict[str, ObservedFile], int, int]:
        """Read regular files through anchored directory descriptors and byte bounds."""
        files: dict[str, ObservedFile] = {}
        ignored = 0
        total = 0

        def fail_scan(error: OSError) -> None:
            """Reject an incomplete walk instead of treating unreadable subtrees as deleted."""
            raise error

        root_descriptor = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for directory, names, filenames, descriptor in os.fwalk(
                ".", follow_symlinks=False, dir_fd=root_descriptor, onerror=fail_scan
            ):
                ignored += sum(
                    stat.S_ISLNK(os.stat(name, dir_fd=descriptor, follow_symlinks=False).st_mode) for name in names
                )
                for name in sorted(filenames):
                    metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                    if not stat.S_ISREG(metadata.st_mode):
                        ignored += 1
                        continue
                    if len(files) >= self.max_files:
                        raise ProductCapacityError("The directory source exceeds its configured regular-file count")
                    if metadata.st_size > self.max_file_bytes:
                        raise ProductCapacityError("A directory-source file exceeds its configured byte limit")
                    file_descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
                    with os.fdopen(file_descriptor, "rb") as stream:
                        before = os.fstat(stream.fileno())
                        if not stat.S_ISREG(before.st_mode):
                            raise ProductConflict("A source file changed type while the directory was being observed")
                        digest = hashlib.sha256()
                        size = 0
                        while chunk := stream.read(64 * 1024):
                            size += len(chunk)
                            total += len(chunk)
                            if size > self.max_file_bytes or total > self.max_total_bytes:
                                raise ProductCapacityError("The directory source exceeds its configured read budget")
                            digest.update(chunk)
                        after = os.fstat(stream.fileno())
                    if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                        after.st_ino,
                        after.st_size,
                        after.st_mtime_ns,
                        after.st_ctime_ns,
                    ):
                        raise ProductConflict("A source file changed during observation. Retry the explicit scan")
                    relative = PurePosixPath(directory, name).as_posix()
                    files[relative] = ObservedFile(
                        relative_path=relative, digest=digest.hexdigest(), size=size, modified_at_ns=after.st_mtime_ns
                    )
        finally:
            os.close(root_descriptor)
        return files, ignored, total

    def observe(self, *, request_id: str) -> ObservationReceipt:
        """Commit a complete explicit scan, with durable request and content deduplication."""
        if not request_id.strip() or len(request_id) > 256:
            raise ValueError("Observation requests require a stable identity of at most 256 characters")
        started = perf_counter()
        key = f"source/observe/{self.source_id}/{request_id}"
        with self.lock:
            with self.store.transaction() as connection:
                previous = self.store.receipt(connection, key, self.configuration_digest, ObservationReceipt)
                if previous is not None:
                    return previous.model_copy(update={"duplicate": True})
                before = self.store.load(connection, "source_states", self.source_id, DirectoryState)
            try:
                files, ignored, total = self.scan_files()
            except OSError as error:
                raise ProductConflict("The configured directory source changed or is unavailable") from error
            checked = datetime.now(UTC)
            with self.store.transaction(write=True) as connection:
                previous = self.store.receipt(connection, key, self.configuration_digest, ObservationReceipt)
                if previous is not None:
                    return previous.model_copy(update={"duplicate": True})
                current = self.store.load(connection, "source_states", self.source_id, DirectoryState)
                if current != before:
                    raise ProductConflict("A newer source scan committed before this observation. Retry explicitly")
                prior = {} if before is None else before.files
                revision = 1 if before is None else before.revision + 1
                sequence = 0 if before is None else before.observation_count
                observations: list[SourceObservation] = []
                for path in sorted(set(prior) | set(files)):
                    old = prior.get(path)
                    value = files.get(path)
                    if old is not None and value is not None and (old.digest, old.size) == (value.digest, value.size):
                        continue
                    sequence += 1
                    observation = SourceObservation(
                        observation_id=f"{self.source_id}:{sequence}",
                        source_id=self.source_id,
                        sequence=sequence,
                        kind="deleted" if value is None else "created" if old is None else "changed",
                        relative_path=path,
                        previous_digest=None if old is None else old.digest,
                        digest=None if value is None else value.digest,
                        size=None if value is None else value.size,
                        observed_at=checked,
                    )
                    self.store.save(connection, "source_observations", observation.observation_id, observation)
                    self.store.emit(connection, self.source_id, "source.observed", observation.model_dump(mode="json"))
                    observations.append(observation)
                state = DirectoryState(
                    source_id=self.source_id,
                    revision=revision,
                    observation_count=sequence,
                    files=files,
                    checked_at=checked,
                )
                self.store.save(connection, "source_states", self.source_id, state)
                receipt = ObservationReceipt(
                    request_id=request_id,
                    source_id=self.source_id,
                    revision=revision,
                    file_count=len(files),
                    ignored_count=ignored,
                    bytes_read=total,
                    observation_ids=tuple(value.observation_id for value in observations),
                    checked_at=checked,
                    duration_seconds=perf_counter() - started,
                )
                self.store.save_receipt(connection, key, self.configuration_digest, receipt)
                return receipt

    def observations(self, *, after_sequence: int = 0, limit: int = 50) -> Page[SourceObservation]:
        """Read bounded source facts without reading the directory again."""
        if after_sequence < 0 or not 1 <= limit <= 100:
            raise ValueError("Invalid source observation page")
        with self.store.transaction() as connection:
            values = tuple(
                value
                for value in self.store.records(connection, "source_observations", SourceObservation)
                if value.source_id == self.source_id and value.sequence > after_sequence
            )
            selected = values[:limit]
            return Page(
                items=selected,
                total=len(values),
                next_cursor=selected[-1].sequence if len(values) > limit else None,
                read_sequence=self.store.sequence(connection),
            )
