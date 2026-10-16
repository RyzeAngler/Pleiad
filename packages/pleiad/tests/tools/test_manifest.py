from pathlib import Path

import pytest

from pleiad.tools.manifest import load_tool_descriptors


def write_manifest(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def test_tool_manifest_loads_only_public_descriptor_fields(tmp_path: Path) -> None:
    manifest = tmp_path / "capabilities.yaml"
    write_manifest(
        manifest,
        "tools:\n  - name: lookup\n    code: records.lookup\n    description: Look up records\n",
    )

    descriptors = load_tool_descriptors(manifest)

    assert descriptors[0].code == "records.lookup"
    assert descriptors[0].name == "lookup"


def test_empty_tool_manifest_is_rejected_as_a_missing_mapping(tmp_path: Path) -> None:
    manifest = tmp_path / "capabilities.yaml"
    write_manifest(manifest, "")

    with pytest.raises(ValueError, match="root must be a mapping"):
        load_tool_descriptors(manifest)


def test_tool_manifest_rejects_duplicate_codes(tmp_path: Path) -> None:
    manifest = tmp_path / "capabilities.yaml"
    write_manifest(
        manifest,
        "tools:\n"
        "  - name: first\n    code: records.lookup\n    description: First\n"
        "  - name: second\n    code: records.lookup\n    description: Second\n",
    )

    with pytest.raises(ValueError, match="duplicate code"):
        load_tool_descriptors(manifest)
