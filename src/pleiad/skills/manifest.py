"""Read Skill descriptors from the same minimal capabilities YAML."""

from pathlib import Path
from typing import Any

import yaml

from pleiad.skills.contracts import SkillDescriptor


def load_skill_descriptors(path: Path) -> tuple[SkillDescriptor, ...]:
    """Load only ``name``, ``code``, and ``description`` Skill entries."""
    loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(loaded, dict):
        raise ValueError("capabilities YAML root must be a mapping")
    values = loaded.get("skills", [])
    if not isinstance(values, list) or not all(isinstance(item, dict) for item in values):
        raise ValueError("capabilities.skills must be a list of mappings")
    return tuple(_descriptor(item) for item in values)


def _descriptor(item: dict[str, Any]) -> SkillDescriptor:
    expected = {"name", "code", "description"}
    unknown = set(item) - expected
    missing = expected - set(item)
    if unknown or missing:
        detail = []
        if missing:
            detail.append(f"missing {sorted(missing)}")
        if unknown:
            detail.append(f"unknown {sorted(unknown)}")
        raise ValueError(f"invalid skills entry: {', '.join(detail)}")
    return SkillDescriptor.model_validate(item)
