"""Revisioned TaskGraph store with compare-and-swap and durable snapshots."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from urllib.parse import quote

from pleiad.errors import ErrorCode, PleiadError, SystemError, make_error
from pleiad.graph.contracts import GraphSpec


@dataclass(frozen=True, slots=True)
class GraphRevision:
    """One stored graph revision."""

    graph: GraphSpec
    created_at: datetime


class GraphConflictError(PleiadError):
    """Raised when a graph update is based on an obsolete revision."""

    def __init__(self, message: str) -> None:
        super().__init__(make_error(ErrorCode.GRAPH_CONFLICT, details={"reason": message}))


class TaskGraphStore:
    """Store validated graph revisions for the active execution."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root
        self._graphs: dict[str, GraphRevision] = {}
        self._history: dict[str, dict[int, GraphRevision]] = {}
        self._lock = RLock()
        if root is not None:
            root.mkdir(parents=True, exist_ok=True)
            self._load()

    def submit(self, graph: GraphSpec, expected_revision: int | None = None) -> GraphRevision:
        """Store a graph using compare-and-swap semantics."""
        with self._lock:
            current = self._graphs.get(graph.graph_id)
            current_revision = current.graph.revision if current else None
            if expected_revision is not None and current_revision != expected_revision:
                raise GraphConflictError(
                    f"图 {graph.graph_id} 期望版本 {expected_revision}, "
                    f"当前版本为 {current_revision}"
                )
            if current and graph.revision <= current.graph.revision:
                raise GraphConflictError("新图版本必须大于当前版本")
            revision = GraphRevision(graph=graph, created_at=datetime.now(UTC))
            if self.root is not None:
                self._persist(revision)
            self._graphs[graph.graph_id] = revision
            self._history.setdefault(graph.graph_id, {})[graph.revision] = revision
            return revision

    def current(self, graph_id: str) -> GraphRevision | None:
        """Return the latest graph revision, if present."""
        with self._lock:
            return self._graphs.get(graph_id)

    def revision(self, graph_id: str, revision: int) -> GraphRevision | None:
        """Return one immutable graph revision, if it exists."""
        with self._lock:
            return self._history.get(graph_id, {}).get(revision)

    def latest(self) -> GraphRevision | None:
        """Return the most recently submitted revision across graph IDs."""
        with self._lock:
            return max(self._graphs.values(), key=lambda item: item.created_at, default=None)

    def _load(self) -> None:
        """Load all durable graph revisions into the in-process indexes."""
        root = self.root
        if root is None:
            return
        for path in sorted(root.glob("*/revision-*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                graph = GraphSpec.model_validate(payload["graph"])
                created_at = datetime.fromisoformat(payload["created_at"])
                stored = GraphRevision(graph=graph, created_at=created_at)
            except Exception as exc:
                raise SystemError(
                    make_error(
                        ErrorCode.SYSTEM_STORAGE,
                        details={"path": str(path), "exception": exc.__class__.__name__},
                    )
                ) from exc
            history = self._history.setdefault(graph.graph_id, {})
            history[graph.revision] = stored
            current = self._graphs.get(graph.graph_id)
            if current is None or graph.revision > current.graph.revision:
                self._graphs[graph.graph_id] = stored

    def _persist(self, revision: GraphRevision) -> None:
        """Atomically write one immutable revision snapshot."""
        root = self.root
        if root is None:
            return
        graph_dir = root / quote(revision.graph.graph_id, safe="")
        graph_dir.mkdir(parents=True, exist_ok=True)
        path = graph_dir / f"revision-{revision.graph.revision}.json"
        temporary = path.with_suffix(".json.tmp")
        payload = {
            "created_at": revision.created_at.isoformat(),
            "graph": revision.graph.model_dump(mode="json"),
        }
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temporary.replace(path)
        except OSError as exc:
            raise SystemError(
                make_error(
                    ErrorCode.SYSTEM_STORAGE,
                    details={"path": str(path), "exception": exc.__class__.__name__},
                )
            ) from exc
