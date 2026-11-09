from typing import Self

from pydantic import Field, model_validator

from pleiad.contracts import ContractModel, UsageCharge
from pleiad.models.result import ModelUsage, sum_tokens


class RunUsage(ContractModel):
    """Measured counts and separate conservative output charges for one Run."""

    input_tokens: int | None = Field(default=0, ge=0)
    output_tokens: int | None = Field(default=0, ge=0)
    total_tokens: int | None = Field(default=0, ge=0)
    budget_output_tokens: int = Field(default=0, ge=0)
    model_calls: int = Field(default=0, ge=0)
    child_model_calls: int = Field(default=0, ge=0)
    child_budget_output_tokens: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)

    def admit_model(self, requested_output_tokens: int) -> "RunUsage":
        """Charge one authorized attempt conservatively before its external request."""
        if requested_output_tokens < 1:
            raise ValueError("A model admission requires a positive output allowance")
        return self.model_copy(
            update={
                "input_tokens": None,
                "output_tokens": None,
                "total_tokens": None,
                "model_calls": self.model_calls + 1,
                "budget_output_tokens": self.budget_output_tokens + requested_output_tokens,
            }
        )

    def add_model(self, usage: ModelUsage, requested_output_tokens: int) -> "RunUsage":
        """Add completed provider measurements to this attempt's original baseline."""
        return RunUsage(
            input_tokens=sum_tokens(self.input_tokens, usage.input_tokens),
            output_tokens=sum_tokens(self.output_tokens, usage.output_tokens),
            total_tokens=sum_tokens(self.total_tokens, usage.total_tokens),
            model_calls=self.model_calls + 1,
            budget_output_tokens=self.budget_output_tokens
            + (requested_output_tokens if usage.output_tokens is None else usage.output_tokens),
            child_model_calls=self.child_model_calls,
            child_budget_output_tokens=self.child_budget_output_tokens,
            tool_calls=self.tool_calls,
        )

    def charge(self, parent_run_id: str, charge_id: str) -> UsageCharge:
        """Project this invocation's inclusive cost into its original child reservation."""
        return UsageCharge(
            parent_run_id=parent_run_id,
            charge_id=charge_id,
            model_steps=self.total_model_calls,
            output_tokens=self.budget_output_tokens,
        )

    @property
    def total_model_calls(self) -> int:
        """Return own model calls plus conservative scoped-child model-step charges."""
        return self.model_calls + self.child_model_calls

    def with_children(self, charges: tuple[UsageCharge, ...]) -> "RunUsage":
        """Replace cumulative child charges while retaining this Run's own measurements."""
        child_tokens = sum(value.output_tokens for value in charges)
        return self.model_copy(
            update={
                "child_model_calls": sum(value.model_steps for value in charges),
                "child_budget_output_tokens": child_tokens,
                "budget_output_tokens": self.budget_output_tokens - self.child_budget_output_tokens + child_tokens,
            }
        )

    @model_validator(mode="after")
    def validate_total(self) -> Self:
        """Validate known measurements and their separate budget charge."""
        if (
            self.total_tokens is not None
            and self.input_tokens is not None
            and self.output_tokens is not None
            and self.total_tokens != self.input_tokens + self.output_tokens
        ):
            raise ValueError("total_tokens must equal input_tokens plus output_tokens")
        if self.budget_output_tokens < self.child_budget_output_tokens:
            raise ValueError("inclusive budget_output_tokens must cover scoped child charges")
        if (
            self.output_tokens is not None
            and self.budget_output_tokens - self.child_budget_output_tokens < self.output_tokens
        ):
            raise ValueError("own budget charges must cover reported output tokens")
        if self.output_tokens is None and self.model_calls > 0 and self.budget_output_tokens == 0:
            raise ValueError("unknown output usage requires a positive budget charge")
        return self
