from pathlib import Path

import pytest

from pleiad.skills.manifest import (
    extract_skill_instructions,
    load_skill_descriptor,
    load_skill_descriptors,
    parse_skill_document,
)


def test_skill_manifest_loads_frontmatter_and_keeps_body_out_of_metadata(tmp_path: Path) -> None:
    """SKILL.md parsing returns the catalog metadata without injecting instructions."""
    package = tmp_path / "demo"
    package.mkdir()
    instructions = package / "SKILL.md"
    instructions.write_text(
        "---\nname: demo\ndescription: Demo skill\nallowed-tools: read_file command\n"
        "metadata:\n  owner: sdk\n---\nsecret body\n",
        encoding="utf-8",
    )

    descriptor = load_skill_descriptor(instructions)

    assert descriptor.code == "demo"
    assert descriptor.allowed_tools == ("read_file", "command")
    assert descriptor.metadata == {"owner": "sdk"}


def test_skill_manifest_extracts_body_as_the_skill_instructions() -> None:
    """The document body is exposed separately from frontmatter metadata."""
    content = "---\nname: demo\ndescription: Demo skill\n---\n\nFollow the guide.\n"

    assert extract_skill_instructions(content) == "Follow the guide."


def test_skill_capability_manifest_loads_only_declared_descriptor_fields(tmp_path: Path) -> None:
    """Capability YAML exposes validated Skill catalog entries."""
    manifest = tmp_path / "capabilities.yaml"
    manifest.write_text(
        "skills:\n  - name: demo\n    code: demo\n    description: Demo skill\n",
        encoding="utf-8",
    )

    descriptors = load_skill_descriptors(manifest)

    assert [(item.code, item.name) for item in descriptors] == [("demo", "demo")]


def test_skill_manifest_rejects_unknown_frontmatter_fields() -> None:
    """A Skill cannot smuggle executable or permission configuration into frontmatter."""
    content = "---\nname: demo\ndescription: Demo skill\ncommand: python check.py\n---\n"

    with pytest.raises(ValueError, match="unknown fields"):
        parse_skill_document(content, code="demo")


def test_skill_manifest_requires_frontmatter_identity_to_match_package_code() -> None:
    """A package whose frontmatter name differs from its selected code is rejected."""
    content = "---\nname: other\ndescription: Demo skill\n---\n"

    with pytest.raises(ValueError, match="match its package code"):
        parse_skill_document(content, code="demo")
