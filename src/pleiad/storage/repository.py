"""Small JSON-backed Execution repository."""

from pathlib import Path
from threading import RLock
from typing import TypeVar, cast

from pleiad.contracts import ContractModel
from pleiad.errors import ErrorCode, SystemError, make_error
from pleiad.storage.models import ExecutionRecord, GraphRunRecord, NodeRunRecord

RecordT = TypeVar("RecordT", bound=ContractModel)


class JsonRecordRepository:
    """Small atomic JSON repository shared by runtime record types."""

    def __init__(self, root: Path, record_type: type[RecordT], key_field: str) -> None:
        self.root = root
        self.record_type = record_type
        self.key_field = key_field
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()

    def save(self, record: RecordT) -> RecordT:
        """Write one record using a same-directory replacement."""
        record_id = str(getattr(record, self.key_field))
        path = self.root / f"{record_id}.json"
        temporary = path.with_suffix(".json.tmp")
        try:
            with self._lock:
                temporary.write_text(record.model_dump_json(indent=2), encoding="utf-8")
                temporary.replace(path)
        except OSError as exc:
            raise SystemError(
                make_error(
                    ErrorCode.SYSTEM_STORAGE,
                    details={"path": str(path), "exception": exc.__class__.__name__},
                )
            ) from exc
        return record

    def get(self, record_id: str) -> RecordT | None:
        """Read one record if it exists."""
        path = self.root / f"{record_id}.json"
        try:
            with self._lock:
                if not path.is_file():
                    return None
                return cast(
                    RecordT,
                    self.record_type.model_validate_json(path.read_text(encoding="utf-8")),
                )
        except (OSError, ValueError) as exc:
            raise SystemError(
                make_error(
                    ErrorCode.SYSTEM_STORAGE,
                    details={"path": str(path), "exception": exc.__class__.__name__},
                )
            ) from exc


class ExecutionRepository(JsonRecordRepository):
    """Persist execution summaries as one atomic-ish JSON file per ID."""

    def __init__(self, root: Path) -> None:
        super().__init__(root, ExecutionRecord, "execution_id")


class GraphRunRepository(JsonRecordRepository):
    """Persist Graph revision runtime state independently from definitions."""

    def __init__(self, root: Path) -> None:
        super().__init__(root, GraphRunRecord, "graph_run_id")


class NodeRunRepository(JsonRecordRepository):
    """Persist temporary Node Agent run state independently from NodeSpec."""

    def __init__(self, root: Path) -> None:
        super().__init__(root, NodeRunRecord, "node_run_id")
