"""Append-only local JSONL TraceStore."""

import json
import re
from pathlib import Path
from threading import RLock

from loguru import logger

from pleiad.observability.models import TraceRecord


class JsonlTraceStore:
    """Store one trace file per Execution for local inspection and replay."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()

    def append(self, record: TraceRecord) -> None:
        """Append one complete JSON record and flush it to the operating system."""
        path = self._path(record.execution_id)
        line = record.as_json()
        with self._lock, path.open("a", encoding="utf-8") as stream:
            stream.write(line)
            stream.write("\n")
            stream.flush()

    def list(self, execution_id: str) -> tuple[TraceRecord, ...]:
        """Read and validate one execution's trace records."""
        path = self._path(execution_id)
        if not path.exists():
            return ()
        records: list[TraceRecord] = []
        with self._lock, path.open(encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, start=1):
                try:
                    value = json.loads(line)
                    if not isinstance(value, dict):
                        raise ValueError("trace record must be a JSON object")
                    records.append(TraceRecord.from_dict(value))
                except (TypeError, ValueError, json.JSONDecodeError, KeyError) as exc:
                    logger.error(
                        "Trace 文件存在无效记录: execution={} line={} error={}",
                        execution_id,
                        line_number,
                        exc,
                    )
        return tuple(sorted(records, key=lambda record: record.sequence))

    def close(self) -> None:
        """Close the stateless file store."""

    def _path(self, execution_id: str) -> Path:
        return self.root / f"{_safe_filename(execution_id)}.jsonl"


def _safe_filename(value: str) -> str:
    """Keep external identifiers from escaping the configured Trace directory."""
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", value)
    return safe or "unknown-execution"
