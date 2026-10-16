import logging
from typing import Literal

from pydantic import ConfigDict, Field, model_validator

from pleiad.contracts import ContractModel, JsonObject, JsonSchema
from pleiad.tools.base import BaseTool, ToolOutput
from pleiad.tools.builtins.file_support import (
    FileToolBinding,
    content_digest,
    normalize_relative_path,
    read_scoped_bytes,
    resolve_read_path,
    select_lines,
)
from pleiad.tools.contracts import ToolContext, ToolDescriptor, ToolPolicy
from pleiad.tools.errors import InvalidToolArgumentsError, ToolFileEncodingError

logger = logging.getLogger(__name__)


class ReadFileArguments(ContractModel):
    """Arguments accepted by the read_file Tool."""

    model_config = ConfigDict(strict=True)

    scope: Literal["workspace", "skill", "scratch"]
    path: str = Field(min_length=1)
    skill_code: str | None = Field(default=None, min_length=1)
    start_line: int = Field(default=1, ge=1)
    max_lines: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_scope_arguments(self) -> "ReadFileArguments":
        """Require a Skill code only when the Skill root is selected."""
        if self.scope == "skill" and self.skill_code is None:
            raise ValueError("skill_code is required for the skill scope")
        if self.scope != "skill" and self.skill_code is not None:
            raise ValueError("skill_code is only valid for the skill scope")
        return self


class ReadFileTool(BaseTool):
    """Read bounded UTF-8 text from one host-bound virtual root."""

    def __init__(self, descriptor: ToolDescriptor, binding: FileToolBinding) -> None:
        """Bind the Tool to host-owned workspace, scratch, and read-only roots."""
        self.descriptor_value = descriptor
        self.binding = binding

    @property
    def descriptor(self) -> ToolDescriptor:
        """Return the stable read_file descriptor."""
        return self.descriptor_value

    @property
    def input_schema(self) -> JsonSchema:
        """Return the schema generated from the single Tool parameter model."""
        return ReadFileArguments.model_json_schema()

    @property
    def required_permissions(self) -> frozenset[str]:
        """Return the grant required for file reads."""
        return frozenset({"file.read"})

    @property
    def skill_codes(self) -> frozenset[str]:
        """Return Skill roots available through this host binding."""
        return frozenset(self.binding.skill_roots)

    @property
    def policy(self) -> ToolPolicy:
        """Return the read Tool execution policy."""
        return ToolPolicy(timeout_seconds=10.0, max_concurrency=16)

    async def execute(self, context: ToolContext, arguments: JsonObject) -> ToolOutput:
        """Read one line-bounded UTF-8 file from a bound relative scope."""
        parsed = ReadFileArguments.model_validate(arguments)
        relative_path = normalize_relative_path(parsed.path, parsed.scope)
        resolved = resolve_read_path(
            self.binding,
            parsed.scope,
            relative_path,
            parsed.skill_code,
        )
        data = read_scoped_bytes(
            resolved,
            scope=parsed.scope,
            relative_path=relative_path,
            max_bytes=self.binding.max_read_bytes,
        )
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ToolFileEncodingError(f"The {parsed.scope} file is not valid UTF-8 text: {relative_path}.") from error
        max_lines = self.binding.max_lines if parsed.max_lines is None else parsed.max_lines
        if max_lines > self.binding.max_lines:
            raise InvalidToolArgumentsError("max_lines exceeds the host file Tool limit.")
        content, line_end, truncated = select_lines(text, parsed.start_line, max_lines)
        digest = content_digest(data)
        logger.info(
            f"file read call_id={context.call_id} scope={parsed.scope} path={relative_path} "
            f"bytes={len(data)} line_start={parsed.start_line} line_end={line_end} truncated={truncated}"
        )
        return {
            "scope": parsed.scope,
            "path": relative_path,
            "line_start": parsed.start_line,
            "line_end": line_end,
            "content": content,
            "truncated": truncated,
            "sha256": digest,
        }
