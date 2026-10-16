from collections.abc import Mapping
from pathlib import Path

import yaml
from pydantic import ValidationError

from pleiad.tools.contracts import ToolDescriptor


def load_tool_descriptors(path: Path) -> tuple[ToolDescriptor, ...]:
    """Load and validate public Tool metadata from a capabilities YAML file.

    Args:
        path: YAML file containing a top-level ``tools`` sequence.

    Returns:
        Validated descriptors in the order declared by the file.

    Raises:
        FileNotFoundError: If the manifest does not exist.
        ValueError: If the YAML root, section, or descriptor is malformed.
    """
    document = load_document(path)
    if "tools" not in document:
        return ()
    entries = document["tools"]
    if not isinstance(entries, list):
        raise ValueError("capabilities.tools must be a list")

    descriptors: list[ToolDescriptor] = []
    codes: set[str] = set()
    for position, entry in enumerate(entries):
        descriptor = parse_descriptor(entry, position)
        if descriptor.code in codes:
            raise ValueError(f"capabilities.tools contains duplicate code: {descriptor.code}")
        codes.add(descriptor.code)
        descriptors.append(descriptor)
    return tuple(descriptors)


def load_document(path: Path) -> Mapping[str, object]:
    """Read a YAML mapping without converting a missing document to defaults."""
    if not path.is_file():
        raise FileNotFoundError(path)
    loaded: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, Mapping):
        raise ValueError("capabilities YAML root must be a mapping")
    if any(not isinstance(key, str) for key in loaded):
        raise ValueError("capabilities YAML keys must be strings")
    return loaded


def parse_descriptor(value: object, position: int) -> ToolDescriptor:
    """Validate one manifest descriptor and report its list position."""
    if not isinstance(value, Mapping):
        raise ValueError(f"invalid tools entry at index {position}: expected a mapping")
    if any(not isinstance(key, str) for key in value):
        raise ValueError(f"invalid tools entry at index {position}: keys must be strings")
    item = dict(value)
    expected = {"name", "code", "description"}
    unknown = set(item) - expected
    missing = expected - set(item)
    if missing:
        raise ValueError(f"invalid tools entry at index {position}: missing {sorted(missing)}")
    if unknown:
        raise ValueError(f"invalid tools entry at index {position}: unknown {sorted(unknown)}")
    try:
        return ToolDescriptor.model_validate(item)
    except ValidationError as error:
        raise ValueError(f"invalid tools entry at index {position}: {error}") from error
