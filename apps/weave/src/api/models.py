from pleiad.contracts import ContractModel
from pydantic import Field

from pleiad_weave.infrastructure.storage.product_models import SpaceConfiguration


class RequestIdentity(ContractModel):
    """Bind a browser mutation to one stable client-generated identity."""

    request_id: str = Field(min_length=1, max_length=256)


class RevisionRequest(RequestIdentity):
    """Reject a browser mutation based on an obsolete object revision."""

    expected_revision: int = Field(ge=1)


class RetryRequest(RevisionRequest):
    """Request a new root Execution with an optional task supplement."""

    supplement: str = Field(default="", max_length=65_536)


class DeliveryRetryRequest(RequestIdentity):
    """Retry the original delivery within its current generation."""

    expected_generation: int = Field(ge=1)


class SpaceUpdate(SpaceConfiguration):
    """Submit the complete allowed Space configuration with compare-and-swap."""

    request_id: str = Field(min_length=1, max_length=256)
    expected_revision: int = Field(ge=1)


class SpaceDelete(RevisionRequest):
    """Accept only a preview-bound deletion impact at its original revision."""

    preview_token: str = Field(min_length=1)
