from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from math import isfinite
from types import MappingProxyType

from pydantic import BaseModel, ConfigDict, Field

type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]
type JsonSchema = JsonObject


@dataclass(frozen=True, slots=True)
class CapabilityGrants:
    """Typed grants for capabilities and resource directions."""

    capabilities: frozenset[str] = frozenset()
    resource_read: frozenset[str] = frozenset()
    resource_write: frozenset[str] = frozenset()

    def intersect(self, other: "CapabilityGrants") -> "CapabilityGrants":
        """Return the least-privileged grant in every independent dimension."""
        return CapabilityGrants(
            capabilities=self.capabilities & other.capabilities,
            resource_read=self.resource_read & other.resource_read,
            resource_write=self.resource_write & other.resource_write,
        )


@dataclass(frozen=True, slots=True)
class ToolRef:
    """Opaque identity of a Tool implementation."""

    code: str

    def __post_init__(self) -> None:
        """Reject empty or oversized references."""
        if not self.code:
            raise ValueError("tool ref code must not be empty")
        if len(self.code) > 256:
            raise ValueError("tool ref code must not exceed 256 characters")


@dataclass(frozen=True, slots=True)
class SkillRef:
    """Opaque identity of static Skill content."""

    code: str

    def __post_init__(self) -> None:
        """Reject empty or oversized references."""
        if not self.code:
            raise ValueError("skill ref code must not be empty")
        if len(self.code) > 256:
            raise ValueError("skill ref code must not exceed 256 characters")


@dataclass(frozen=True, slots=True, init=False)
class FrozenJsonMapping(Mapping[str, object]):
    """Recursively immutable JSON object used at long-lived boundaries."""

    data: Mapping[str, object]

    def __init__(self, value: Mapping[str, object]) -> None:
        """Validate, detach, and recursively freeze a JSON object.

        Args:
            value: Mapping to validate and freeze.

        Raises:
            TypeError: If the object is not a mapping.
            ValueError: If keys or values are outside the JSON contract.
        """
        if not isinstance(value, Mapping):
            raise TypeError("JSON object must be a mapping")

        active: set[int] = set()

        def freeze_mapping(mapping: Mapping[str, object]) -> Mapping[str, object]:
            identity = id(mapping)
            if identity in active:
                raise ValueError("JSON values cannot contain circular references")
            active.add(identity)
            try:
                frozen: dict[str, object] = {}
                for key, item in mapping.items():
                    if not isinstance(key, str):
                        raise ValueError("JSON object keys must be strings")
                    frozen[key] = freeze_value(item)
                return MappingProxyType(frozen)
            finally:
                active.remove(identity)

        def freeze_value(item: object) -> object:
            if isinstance(item, Mapping):
                return freeze_mapping(item)
            if isinstance(item, list | tuple):
                identity = id(item)
                if identity in active:
                    raise ValueError("JSON values cannot contain circular references")
                active.add(identity)
                try:
                    return tuple(freeze_value(child) for child in item)
                finally:
                    active.remove(identity)
            if item is None or isinstance(item, str | bool | int):
                return item
            if isinstance(item, float) and isfinite(item):
                return item
            raise ValueError("JSON values must be finite scalars, mappings, lists, or tuples")

        object.__setattr__(self, "data", freeze_mapping(value))

    def __getitem__(self, key: str) -> object:
        return self.data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.data)

    def __len__(self) -> int:
        return len(self.data)


def freeze_json_object(value: Mapping[str, object]) -> FrozenJsonMapping:
    """Return a recursively copied and immutable JSON object."""
    return FrozenJsonMapping(value)


def thaw_json_object(value: Mapping[str, object]) -> JsonObject:
    """Return a detached JSON object with dictionaries and lists."""
    frozen = FrozenJsonMapping(value)

    def thaw(item: object) -> JsonValue:
        if isinstance(item, Mapping):
            return {key: thaw(child) for key, child in item.items()}
        if isinstance(item, tuple | list):
            return [thaw(child) for child in item]
        if item is None or isinstance(item, str | bool | int | float):
            return item
        raise ValueError("JSON values must be finite scalars, mappings, lists, or tuples")

    return {key: thaw(item) for key, item in frozen.items()}


class ContractModel(BaseModel):
    """Immutable model base shared by non-LLM domain contracts."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        validate_default=True,
    )


class UsageCharge(ContractModel):
    """Identify cumulative scoped-child budget charges independently of model observations."""

    parent_run_id: str = Field(min_length=1)
    charge_id: str = Field(min_length=1)
    model_steps: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
