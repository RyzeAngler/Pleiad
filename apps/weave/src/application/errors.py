from pleiad.contracts import ContractModel
from pleiad.errors.base import ErrorType, PleiadError
from pydantic import Field


class ErrorRecord(ContractModel):
    """Serialize a classified failure in a durable Weave product record."""

    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    type: ErrorType

    @classmethod
    def from_exception(cls, error: PleiadError) -> "ErrorRecord":
        """Project an SDK exception without persisting its traceback or credentials."""
        return cls(code=error.code, message=error.message, type=error.type)
