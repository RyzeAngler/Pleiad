import hashlib
from pathlib import Path

import pytest

from pleiad_weave.infrastructure.config import Settings
from pleiad_weave.infrastructure.storage.observations import DirectoryObserver
from pleiad_weave.infrastructure.storage.product import ProductStore
from pleiad_weave.infrastructure.storage.product_models import ProductCapacityError, ProductConflict


def test_real_directory_changes_are_deduplicated_across_requests_and_store_restarts(tmp_path: Path) -> None:
    external = tmp_path / "external"
    external.mkdir()
    source = external / "note.txt"
    source.write_bytes(b"first external contents")
    store = ProductStore(tmp_path / "runtime")
    observer = DirectoryObserver(store, external)

    first = observer.observe(request_id="first")
    duplicate = observer.observe(request_id="first")
    unchanged = DirectoryObserver(ProductStore(store.root), external).observe(request_id="unchanged")

    assert first.file_count == 1 and first.bytes_read == len(b"first external contents")
    assert first.observation_ids and duplicate.duplicate and duplicate.observation_ids == first.observation_ids
    assert unchanged.observation_ids == ()
    source.write_bytes(b"changed external contents")
    changed = DirectoryObserver(ProductStore(store.root), external).observe(request_id="changed")
    source.unlink()
    deleted = observer.observe(request_id="deleted")
    observations = observer.observations().items
    assert [value.kind for value in observations] == ["created", "changed", "deleted"]
    assert [value.sequence for value in observations] == [1, 2, 3]
    assert observations[0].digest == hashlib.sha256(b"first external contents").hexdigest()
    assert observations[1].previous_digest == observations[0].digest
    assert observations[2].previous_digest == observations[1].digest and observations[2].digest is None
    assert changed.observation_ids == (observations[1].observation_id,)
    assert deleted.observation_ids == (observations[2].observation_id,)
    assert b"external contents" not in observer.observations().model_dump_json().encode()
    assert store.list_executions().items == () and store.messages().items == ()


def test_directory_scan_never_follows_symlinks_and_preserves_external_bytes(tmp_path: Path) -> None:
    external = tmp_path / "external"
    external.mkdir()
    outside = tmp_path / "private.txt"
    outside.write_bytes(b"private bytes")
    (external / "link.txt").symlink_to(outside)
    (external / "regular.txt").write_bytes(b"authorized bytes")
    observer = DirectoryObserver(ProductStore(tmp_path / "runtime"), external)

    receipt = observer.observe(request_id="scan")

    assert receipt.file_count == 1 and receipt.ignored_count == 1
    assert receipt.bytes_read == len(b"authorized bytes")
    assert [value.relative_path for value in observer.observations().items] == ["regular.txt"]
    assert outside.read_bytes() == b"private bytes" and (external / "regular.txt").read_bytes() == b"authorized bytes"
    alias = tmp_path / "alias"
    alias.symlink_to(external, target_is_directory=True)
    forbidden = DirectoryObserver(observer.store, alias)
    assert forbidden.health().state == "invalid"
    with pytest.raises(ProductConflict):
        forbidden.observe(request_id="reject-root-link")


def test_directory_capacity_failure_does_not_commit_a_partial_observation(tmp_path: Path) -> None:
    external = tmp_path / "external"
    external.mkdir()
    (external / "a.txt").write_bytes(b"one")
    observer = DirectoryObserver(ProductStore(tmp_path / "runtime"), external, max_total_bytes=4)
    first = observer.observe(request_id="first")
    (external / "b.txt").write_bytes(b"two")

    with pytest.raises(ProductCapacityError, match="read budget"):
        observer.observe(request_id="oversized")

    assert tuple(value.observation_id for value in observer.observations().items) == first.observation_ids
    (external / "b.txt").unlink()
    retry = observer.observe(request_id="oversized")
    assert retry.observation_ids == () and retry.revision == first.revision + 1


def test_source_health_runs_only_when_requested_and_reports_actual_missing_directory(tmp_path: Path) -> None:
    external = tmp_path / "external"
    external.mkdir()
    observer = DirectoryObserver(ProductStore(tmp_path / "runtime"), external)
    assert observer.health().available
    external.rmdir()
    health = observer.health()
    assert health.state == "missing" and not health.available
    assert health.duration_seconds >= 0 and observer.observations().items == ()


def test_directory_source_defaults_disabled_and_blank_host_configuration_stays_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PLEIAD_OBSERVE_DIRECTORY", raising=False)
    image = f"python@sha256:{'a' * 64}"
    selected = Settings(workspace=tmp_path, sandbox_image=image)
    blank = Settings.model_validate({"workspace": str(tmp_path), "sandbox_image": image, "observe_directory": ""})
    assert selected.observe_directory is None and blank.observe_directory is None
