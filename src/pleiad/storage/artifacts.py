"""Content-addressed JSON artifact storage."""

import hashlib
import json
from pathlib import Path
from uuid import uuid4

from pleiad.errors import ErrorCode, SystemError, make_error
from pleiad.storage.models import ArtifactRef


class ArtifactStore:
    """Store structured Node outputs outside Agent message history."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def put_json(self, value: object) -> ArtifactRef:
        """Write a canonical JSON payload and return its stable reference."""
        payload = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        digest = hashlib.sha256(payload).hexdigest()
        artifact_id = uuid4().hex
        path = self.root / f"{artifact_id}.json"
        try:
            path.write_bytes(payload)
        except OSError as exc:
            raise SystemError(
                make_error(
                    ErrorCode.SYSTEM_STORAGE,
                    details={"path": str(path), "exception": exc.__class__.__name__},
                )
            ) from exc
        return ArtifactRef(
            artifact_id=artifact_id,
            media_type="application/json",
            digest=digest,
            location=str(path),
        )

    def get_json(self, reference: ArtifactRef) -> object:
        """Read and verify one JSON artifact."""
        try:
            payload = Path(reference.location).read_bytes()
            if hashlib.sha256(payload).hexdigest() != reference.digest:
                raise ValueError(f"artifact digest mismatch: {reference.artifact_id}")
            return json.loads(payload)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise SystemError(
                make_error(
                    ErrorCode.SYSTEM_STORAGE,
                    details={
                        "artifact_id": reference.artifact_id,
                        "exception": exc.__class__.__name__,
                    },
                )
            ) from exc
