"""Revisioned TaskGraph storage for the Pleiad Weave application."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from threading import RLock

from pleiad.errors.base import BusinessError, SystemFault

from pleiad_weave.agent.contracts import GraphSpec


@dataclass(frozen=True, slots=True)
class GraphRevision:
    """One stored graph revision."""

    graph: GraphSpec
    created_at: datetime


class GraphConflictError(BusinessError):
    """Raised when a graph update is based on an obsolete revision."""

    def __init__(self, message: str) -> None:
        super().__init__("weave.graph_conflict", str({"reason": message}))


class TaskGraphStore:
    """Store validated graph revisions for the active execution."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root
        self.graphs: dict[str, GraphRevision] = {}
        self.history: dict[str, dict[int, GraphRevision]] = {}
        self.lock = RLock()
        if root is not None:
            root.mkdir(parents=True, exist_ok=True)
            self.load()

    def submit(self, graph: GraphSpec, expected_revision: int | None = None) -> GraphRevision:
        """Store a graph using compare-and-swap semantics."""
        with self.lock:
            current = self.graphs.get(graph.graph_id)
            current_revision = current.graph.revision if current else None
            if expected_revision is not None and current_revision != expected_revision:
                raise GraphConflictError(
                    f"图 {graph.graph_id} 期望版本 {expected_revision}, 当前版本为 {current_revision}"
                )
            if current and graph.revision <= current.graph.revision:
                raise GraphConflictError("新图版本必须大于当前版本")
            revision = GraphRevision(graph=graph, created_at=datetime.now(UTC))
            if self.root is not None:
                self.persist(revision)
            self.graphs[graph.graph_id] = revision
            self.history.setdefault(graph.graph_id, {})[graph.revision] = revision
            return revision

    def current(self, graph_id: str) -> GraphRevision | None:
        """Return the latest graph revision, if present."""
        with self.lock:
            return self.graphs.get(graph_id)

    def revision(self, graph_id: str, revision: int) -> GraphRevision | None:
        """Return one immutable graph revision, if it exists."""
        with self.lock:
            return self.history.get(graph_id, {}).get(revision)

    def latest(self) -> GraphRevision | None:
        """Return the most recently submitted revision across graph IDs."""
        with self.lock:
            return max(self.graphs.values(), key=lambda item: item.created_at, default=None)

    def delete_revision(self, graph_id: str, revision: int) -> None:
        """Remove an unreferenced revision from disk and the live Graph indexes."""
        with self.lock:
            if self.root is not None:
                path = self.root / sha256(graph_id.encode("utf-8")).hexdigest() / f"revision-{revision}.json"
                try:
                    path.unlink(missing_ok=True)
                except OSError as error:
                    raise SystemFault(
                        "weave.system_storage", str({"path": str(path), "exception": type(error).__name__})
                    ) from error
            history = self.history.get(graph_id)
            if history is None:
                return
            history.pop(revision, None)
            if history:
                self.graphs[graph_id] = history[max(history)]
            else:
                self.history.pop(graph_id, None)
                self.graphs.pop(graph_id, None)

    def load(self) -> None:
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
                raise SystemFault(
                    "weave.system_storage", str({"path": str(path), "exception": exc.__class__.__name__})
                ) from exc
            history = self.history.setdefault(graph.graph_id, {})
            history[graph.revision] = stored
            current = self.graphs.get(graph.graph_id)
            if current is None or graph.revision > current.graph.revision:
                self.graphs[graph.graph_id] = stored

    def persist(self, revision: GraphRevision) -> None:
        """Atomically write one immutable revision snapshot."""
        root = self.root
        if root is None:
            return
        graph_dir = root / sha256(revision.graph.graph_id.encode("utf-8")).hexdigest()
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
            raise SystemFault(
                "weave.system_storage", str({"path": str(path), "exception": exc.__class__.__name__})
            ) from exc
