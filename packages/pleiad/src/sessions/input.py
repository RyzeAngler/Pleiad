import hashlib
import json
from datetime import datetime
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, field_validator

from pleiad.contracts import ContractModel
from pleiad.messages.images import ImageBlock
from pleiad.messages.message import TextBlock, UserMessage
from pleiad.messages.results import ResultReference


class InputEnvelope(ContractModel):
    """Validate one immutable host-originated Session input."""

    input_id: str = Field(min_length=1, max_length=256)
    kind: Literal["user", "observation", "delegate_result", "operation_result"]
    source: str = Field(min_length=1, max_length=512)
    content: str | ResultReference | tuple[Annotated[TextBlock | ImageBlock, Field(discriminator="type")], ...]
    causation_id: str | None = Field(default=None, min_length=1, max_length=256)

    @field_validator("content")
    @classmethod
    def validate_content(
        cls, value: str | ResultReference | tuple[TextBlock | ImageBlock, ...]
    ) -> str | ResultReference | tuple[TextBlock | ImageBlock, ...]:
        """Reject an empty accepted input before claiming it into a Run."""
        if isinstance(value, str) and not value.strip():
            raise ValueError("Session inputs require nonempty content")
        if isinstance(value, tuple) and not value:
            raise ValueError("Session inputs require at least one content block")
        return value

    def payload_digest(self) -> str:
        """Hash normalized content independently of delivery attempts."""
        payload = self.model_dump(mode="json", exclude={"input_id"})
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def message(self) -> UserMessage:
        """Project this input into its stable model-visible message identity."""
        if isinstance(self.content, tuple):
            metadata = self.model_dump_json(exclude={"content"})
            return UserMessage(message_id=self.input_id, content=(TextBlock(text=metadata), *self.content))
        return UserMessage(message_id=self.input_id, content=self.model_dump_json())


class InputReceipt(ContractModel):
    """Acknowledge acceptance while preserving the original identity and time."""

    input_id: str
    sequence: int = Field(ge=1)
    accepted_at: datetime
    duplicate: bool


class InputRecord(ContractModel):
    """Persist input acceptance, claim, and model-step consumption separately."""

    envelope: InputEnvelope
    payload_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    sequence: int = Field(ge=1)
    accepted_at: AwareDatetime
    run_id: str | None = None
    step_id: str | None = None

    def receipt(self, *, duplicate: bool) -> InputReceipt:
        """Return the original acceptance details for this delivery."""
        return InputReceipt(
            input_id=self.envelope.input_id,
            sequence=self.sequence,
            accepted_at=self.accepted_at,
            duplicate=duplicate,
        )
