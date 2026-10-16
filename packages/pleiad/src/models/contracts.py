from pydantic import Field

from pleiad.contracts import ContractModel, JsonObject


class Tool(ContractModel):
    """Provider-neutral tool schema supplied to a model request."""

    name: str = Field(min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=16_000)
    input_schema: JsonObject
    strict: bool = False
