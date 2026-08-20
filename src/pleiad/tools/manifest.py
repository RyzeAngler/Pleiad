"""Read the intentionally small user-facing capabilities YAML."""

from pathlib import Path
from typing import Any

import yaml

from pleiad.tools.contracts import ToolDescriptor


def load_tool_descriptors(path: Path) -> tuple[ToolDescriptor, ...]:
    """Load only ``name``, ``code``, and ``description`` Tool entries."""
    document = _load_document(path)
    return tuple(_descriptor(item, "tools") for item in _entries(document, "tools"))


def _load_document(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(path)
    loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(loaded, dict):
        raise ValueError("capabilities YAML root must be a mapping")
    return loaded


def _entries(document: dict[str, Any], key: str) -> list[dict[str, Any]]:
    values = document.get(key, [])
    if not isinstance(values, list):
        raise ValueError(f"capabilities.{key} must be a list")
    if not all(isinstance(item, dict) for item in values):
        raise ValueError(f"capabilities.{key} entries must be mappings")
    return values


def _descriptor(item: dict[str, Any], section: str) -> ToolDescriptor:
    expected = {"name", "code", "description"}
    unknown = set(item) - expected
    missing = expected - set(item)
    if unknown or missing:
        detail = []
        if missing:
            detail.append(f"missing {sorted(missing)}")
        if unknown:
            detail.append(f"unknown {sorted(unknown)}")
        raise ValueError(f"invalid {section} entry: {', '.join(detail)}")
    return ToolDescriptor.model_validate(item)
