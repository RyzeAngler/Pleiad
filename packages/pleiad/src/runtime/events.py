from dataclasses import dataclass

from pleiad.contracts import JsonObject


@dataclass(frozen=True, slots=True)
class RunEvent:
    """Associate one persisted observation with its replay cursor."""

    sequence: int
    event: JsonObject
