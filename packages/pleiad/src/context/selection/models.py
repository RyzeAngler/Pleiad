from dataclasses import dataclass

from pydantic import ConfigDict, Field

from pleiad.contracts import ContractModel, JsonObject


class ContextBlock(ContractModel):
    """One request-local reference block supplied by an SDK caller."""

    model_config = ConfigDict(strict=True)

    kind: str = Field(min_length=1, max_length=64)
    origin: str = Field(min_length=1, max_length=512)
    content: str = Field(min_length=1, max_length=200_000)
    relevance: float | None = Field(default=None, allow_inf_nan=False)
    metadata: JsonObject = Field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ContextView:
    """Immutable ordered dynamic context for one model request."""

    blocks: tuple[ContextBlock, ...] = ()
