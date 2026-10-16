from collections.abc import Mapping
from pathlib import Path

import yaml

from pleiad.skills.contracts import SKILL_SLUG_PATTERN, SkillDescriptor, is_skill_slug

FRONTMATTER_FIELDS = {
    "name",
    "description",
    "license",
    "compatibility",
    "metadata",
    "allowed-tools",
}


def load_skill_descriptors(path: Path) -> tuple[SkillDescriptor, ...]:
    """Load Skill metadata from a capabilities YAML file."""
    loaded: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    root = require_mapping(loaded, "capabilities YAML root")
    values = root.get("skills", [])
    if not isinstance(values, list):
        raise ValueError("capabilities.skills must be a list")
    descriptors: list[SkillDescriptor] = []
    for index, value in enumerate(values):
        item = require_mapping(value, f"capabilities.skills[{index}]")
        descriptors.append(descriptor_from_mapping(item))
    return tuple(descriptors)


def descriptor_from_mapping(item: Mapping[str, object]) -> SkillDescriptor:
    """Validate one capability descriptor mapping."""
    allowed = {"name", "code", "description", "license", "compatibility", "metadata", "allowed-tools"}
    missing = {"name", "code", "description"} - set(item)
    unknown = set(item) - allowed
    if missing:
        raise ValueError(f"invalid skills entry: missing {sorted(missing)}")
    if unknown:
        raise ValueError(f"invalid skills entry: unknown {sorted(unknown)}")
    values = dict(item)
    if "allowed-tools" in values:
        values["allowed_tools"] = normalize_allowed_tools(values.pop("allowed-tools"))
    validate_skill_code(values.get("code"))
    return SkillDescriptor.model_validate(values)


def load_skill_descriptor(instructions_path: Path) -> SkillDescriptor:
    """Parse one package's SKILL.md frontmatter without loading its body."""
    package_code = instructions_path.parent.name
    validate_skill_code(package_code)
    return parse_skill_document(instructions_path.read_text(encoding="utf-8"), code=package_code)


def parse_skill_document(content: str, *, code: str) -> SkillDescriptor:
    """Parse and validate the YAML frontmatter of one SKILL.md document."""
    validate_skill_code(code)
    lines, end = split_frontmatter(content)
    try:
        loaded: object = yaml.safe_load("\n".join(lines[1:end]))
    except yaml.YAMLError as error:
        raise ValueError("SKILL.md frontmatter is invalid YAML") from error
    values = require_mapping(loaded, "SKILL.md frontmatter")
    missing = {"name", "description"} - set(values)
    unknown = set(values) - FRONTMATTER_FIELDS
    if missing:
        raise ValueError(f"SKILL.md frontmatter missing {sorted(missing)}")
    if unknown:
        raise ValueError(f"SKILL.md frontmatter has unknown fields {sorted(unknown)}")
    name = values.get("name")
    if not isinstance(name, str) or name != code:
        raise ValueError("SKILL.md frontmatter name must match its package code")
    normalized = dict(values)
    normalized["code"] = code
    if "allowed-tools" in normalized:
        normalized["allowed_tools"] = normalize_allowed_tools(normalized.pop("allowed-tools"))
    return SkillDescriptor.model_validate(normalized)


def extract_skill_instructions(content: str) -> str:
    """Return the non-empty Markdown body after SKILL.md frontmatter."""
    lines, end = split_frontmatter(content)
    instructions = "\n".join(lines[end + 1 :]).strip()
    if not instructions:
        raise ValueError("SKILL.md instructions must not be empty")
    return instructions


def split_frontmatter(content: str) -> tuple[list[str], int]:
    """Split one SKILL.md document at its closed YAML frontmatter."""
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("SKILL.md must start with YAML frontmatter")
    try:
        end = next(index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---")
    except StopIteration as error:
        raise ValueError("SKILL.md frontmatter is not closed") from error
    return lines, end


def normalize_allowed_tools(value: object) -> tuple[str, ...]:
    """Normalize the optional standard allowed-tools frontmatter value."""
    if isinstance(value, str):
        values = tuple(value.split())
    elif isinstance(value, list) and all(isinstance(item, str) for item in value):
        values = tuple(value)
    else:
        raise ValueError("allowed-tools must be whitespace-delimited text or a list of strings")
    if any(not item for item in values):
        raise ValueError("allowed-tools entries must be non-empty")
    return values


def validate_skill_code(value: object) -> None:
    """Reject a Skill code that cannot be used as a virtual relative path."""
    if not isinstance(value, str) or not is_skill_slug(value):
        raise ValueError(f"Skill code must match {SKILL_SLUG_PATTERN}")


def require_mapping(value: object, label: str) -> dict[str, object]:
    """Convert a parsed mapping to a string-keyed object mapping."""
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a mapping")
    result: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise ValueError(f"{label} keys must be strings")
        result[key] = item
    return result
