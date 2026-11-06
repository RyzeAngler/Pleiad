"""Content-addressed JSON artifact storage."""

import hashlib
import json
from pathlib import Path
from uuid import uuid4

from pleiad.contracts import JsonObject
from pleiad.errors.base import SystemFault
from pydantic import TypeAdapter

from pleiad_weave.infrastructure.storage.models import ArtifactRef

JSON_OBJECT_ADAPTER: TypeAdapter[JsonObject] = TypeAdapter(JsonObject)


class ArtifactStore:
    """Store structured Node outputs outside Agent message history."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def put_json(self, value: JsonObject) -> ArtifactRef:
        """Write a canonical JSON payload and return its stable reference."""
        payload = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")
        digest = hashlib.sha256(payload).hexdigest()
        artifact_id = uuid4().hex
        path = self.root / f"{artifact_id}.json"
        try:
            path.write_bytes(payload)
        except OSError as exc:
            raise SystemFault(
                "weave.system_storage", str({"path": str(path), "exception": exc.__class__.__name__})
            ) from exc
        return ArtifactRef(
            artifact_id=artifact_id,
            media_type="application/json",
            digest=digest,
            location=str(path),
        )

    def get_json(self, reference: ArtifactRef) -> JsonObject:
        """Read and verify one JSON artifact."""
        try:
            payload = Path(reference.location).read_bytes()
            if hashlib.sha256(payload).hexdigest() != reference.digest:
                raise ValueError(f"artifact digest mismatch: {reference.artifact_id}")
            return JSON_OBJECT_ADAPTER.validate_json(payload)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise SystemFault(
                "weave.system_storage", str({"artifact_id": reference.artifact_id, "exception": exc.__class__.__name__})
            ) from exc

    def delete(self, artifact_id: str) -> None:
        """Remove an owned artifact using its identity, never its stored location."""
        path = self.root / f"{artifact_id}.json"
        if path.parent != self.root:
            raise ValueError("Artifact identity must not contain path components")
        try:
            path.unlink(missing_ok=True)
        except OSError as error:
            raise SystemFault(
                "weave.system_storage", str({"artifact_id": artifact_id, "exception": type(error).__name__})
            ) from error
