import hashlib

from pydantic import Field

from pleiad.contracts import ContractModel, JsonObject
from pleiad.messages.message import ToolMessage

TOOL_RESULT_PAGE_CHARS = 4_000
MAX_TOOL_RESULT_PAGE_CHARS = 8_000
MAX_INLINE_TOOL_RESULT_CHARS = 64_000


class ToolResultReference(ContractModel):
    """Identify an immutable Tool observation in the current ShortMemory.

    The host owns the memory scope and its retention. A reference never grants
    access to another conversation or to an arbitrary filesystem location.
    """

    message_id: str = Field(min_length=1, max_length=256)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    total_chars: int = Field(ge=0)

    @classmethod
    def from_message(cls, message: ToolMessage) -> "ToolResultReference":
        """Describe the complete persisted observation without copying its body.

        Args:
            message: Original Tool message retained by ShortMemory.

        Returns:
            Its identity, UTF-8 digest, and character count.
        """
        return cls(
            message_id=message.message_id,
            sha256=hashlib.sha256(message.content.encode("utf-8")).hexdigest(),
            total_chars=len(message.content),
        )

    def read_arguments(self) -> JsonObject:
        """Return model-visible arguments for reading the first bounded page.

        Returns:
            Arguments accepted by the Run-scoped read_tool_result Tool.
        """
        return {"message_id": self.message_id, "start_char": 0, "max_chars": TOOL_RESULT_PAGE_CHARS}
