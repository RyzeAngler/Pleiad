import logging
from typing import Literal

from pydantic import ConfigDict, Field, model_validator

from pleiad.contracts import ContractModel, JsonObject, JsonSchema
from pleiad.tools.base import BaseTool, ToolOutput
from pleiad.tools.builtins.file_support import (
    FileToolBinding,
    content_digest,
    ensure_parent_directory,
    normalize_relative_path,
    read_scoped_bytes,
    resolve_scoped_path,
    write_atomic,
)
from pleiad.tools.contracts import ToolContext, ToolDescriptor, ToolPolicy
from pleiad.tools.errors import (
    InvalidToolArgumentsError,
    ToolFileAccessDeniedError,
    ToolFileConflictError,
    ToolFileNotFoundError,
    ToolFileTooLargeError,
)

logger = logging.getLogger(__name__)


class WriteFileArguments(ContractModel):
    """Arguments accepted by the write_file Tool."""

    model_config = ConfigDict(strict=True)

    path: str = Field(min_length=1)
    content: str
    mode: Literal["create", "replace"] = "create"
    expected_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_write_mode(self) -> "WriteFileArguments":
        """Require a digest only for conditional replacement."""
        if self.mode == "replace" and self.expected_sha256 is None:
            raise ValueError("replace requires expected_sha256")
        if self.mode == "create" and self.expected_sha256 is not None:
            raise ValueError("create must not include expected_sha256")
        return self


class WriteFileTool(BaseTool):
    """Create or conditionally replace bounded UTF-8 workspace text."""

    def __init__(self, descriptor: ToolDescriptor, binding: FileToolBinding) -> None:
        """Bind the Tool to one host-owned workspace root."""
        self.descriptor_value = descriptor
        self.binding = binding

    @property
    def descriptor(self) -> ToolDescriptor:
        """Return the stable write_file descriptor."""
        return self.descriptor_value

    @property
    def input_schema(self) -> JsonSchema:
        """Return the schema generated from the single Tool parameter model."""
        return WriteFileArguments.model_json_schema()

    @property
    def required_permissions(self) -> frozenset[str]:
        """Return the grant required for workspace writes."""
        return frozenset({"file.write"})

    @property
    def policy(self) -> ToolPolicy:
        """Return the serialized write Tool execution policy."""
        return ToolPolicy(timeout_seconds=10.0, max_concurrency=1)

    async def execute(self, context: ToolContext, arguments: JsonObject) -> ToolOutput:
        """Write one workspace file using create or digest-checked replacement."""
        parsed = WriteFileArguments.model_validate(arguments)
        relative_path = normalize_relative_path(parsed.path, "workspace")
        try:
            data = parsed.content.encode("utf-8")
        except UnicodeEncodeError as error:
            raise InvalidToolArgumentsError("content must be valid UTF-8 text.") from error
        if len(data) > self.binding.max_write_bytes:
            raise ToolFileTooLargeError(
                f"The workspace file exceeds the {self.binding.max_write_bytes} byte limit: "
                f"{relative_path} ({len(data)} bytes)."
            )
        target = resolve_scoped_path(self.binding.workspace, relative_path, "workspace")
        ensure_parent_directory(self.binding.workspace, target, relative_path)
        current_digest: str | None = None
        if target.exists():
            if not target.is_file() or target.is_symlink():
                raise ToolFileAccessDeniedError(
                    f"The workspace path is not an accessible regular file: {relative_path}."
                )
            current_data = read_scoped_bytes(
                target,
                scope="workspace",
                relative_path=relative_path,
                max_bytes=self.binding.max_write_bytes,
            )
            current_digest = content_digest(current_data)
        if parsed.mode == "create" and current_digest is not None:
            raise ToolFileConflictError(f"The workspace file already exists: {relative_path}.")
        if parsed.mode == "replace":
            if current_digest is None:
                raise ToolFileNotFoundError(f"The workspace file does not exist: {relative_path}.")
            if current_digest != parsed.expected_sha256:
                raise ToolFileConflictError(f"The workspace file changed before replacement: {relative_path}.")
        write_atomic(
            target,
            data,
            create=parsed.mode == "create",
            relative_path=relative_path,
        )
        digest = content_digest(data)
        logger.info(f"file write call_id={context.call_id} path={relative_path} bytes={len(data)} mode={parsed.mode}")
        return {"path": relative_path, "bytes_written": len(data), "sha256": digest}
