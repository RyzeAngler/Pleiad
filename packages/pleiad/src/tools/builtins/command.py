import logging
from pathlib import PurePosixPath

from pydantic import ConfigDict, Field, field_validator

from pleiad.contracts import ContractModel, JsonObject, JsonSchema
from pleiad.sandbox.models import SandboxMount, SandboxRequest, SandboxResult
from pleiad.sandbox.policy import SandboxPolicy
from pleiad.sandbox.sandbox import Sandbox
from pleiad.tools.base import BaseTool, ToolOutput
from pleiad.tools.builtins.file_support import FileToolBinding
from pleiad.tools.contracts import ToolContext, ToolDescriptor, ToolPolicy

logger = logging.getLogger(__name__)

COMMAND_CWD = PurePosixPath("/pleiad")
WORKSPACE_TARGET = PurePosixPath("/pleiad/workspace")
SKILLS_TARGET = PurePosixPath("/pleiad/skills")
SCRATCH_TARGET = PurePosixPath("/pleiad/scratch")


class CommandArguments(ContractModel):
    """Arguments accepted by the command Tool."""

    model_config = ConfigDict(strict=True)

    command: str = Field(min_length=1)

    @field_validator("command")
    @classmethod
    def validate_command(cls, value: str) -> str:
        """Reject commands containing no executable text."""
        if not value.strip():
            raise ValueError("command must be non-empty text")
        return value


class CommandTool(BaseTool):
    """Run model-provided shell text inside the host-selected Sandbox."""

    def __init__(
        self,
        descriptor: ToolDescriptor,
        binding: FileToolBinding,
        sandbox: Sandbox,
        policy: SandboxPolicy,
    ) -> None:
        """Bind command execution to explicit roots, Sandbox, and policy."""
        if not policy.allow_shell_command:
            raise ValueError("command Tool requires a SandboxPolicy that allows shell commands")
        self.descriptor_value = descriptor
        self.binding = binding
        self.sandbox = sandbox
        self.sandbox_policy = policy

    @property
    def descriptor(self) -> ToolDescriptor:
        """Return the stable command descriptor."""
        return self.descriptor_value

    @property
    def input_schema(self) -> JsonSchema:
        """Return the schema generated from the single Tool parameter model."""
        return CommandArguments.model_json_schema()

    @property
    def required_permissions(self) -> frozenset[str]:
        """Return the grant required for Sandbox commands."""
        return frozenset({"sandbox.command"})

    @property
    def policy(self) -> ToolPolicy:
        """Return the Tool timeout aligned with the Sandbox policy."""
        return ToolPolicy(timeout_seconds=self.sandbox_policy.timeout_seconds, max_concurrency=1)

    async def execute(self, context: ToolContext, arguments: JsonObject) -> ToolOutput:
        """Execute command text with workspace and read-only roots mounted."""
        parsed = CommandArguments.model_validate(arguments)
        request = SandboxRequest(
            cwd=COMMAND_CWD,
            mounts=build_mounts(self.binding),
            policy=self.sandbox_policy,
            shell_command=parsed.command,
        )
        result = await self.sandbox.execute(request)
        stdout, stdout_loss = decode_output(result.stdout)
        stderr, stderr_loss = decode_output(result.stderr)
        decoding_loss = stdout_loss or stderr_loss
        logger.info(
            f"command completed call_id={context.call_id} backend={result.backend} exit_code={result.exit_code} "
            f"timed_out={result.timed_out} output_truncated={result.output_truncated}"
        )
        return command_output(result, stdout, stderr, decoding_loss)


def build_mounts(binding: FileToolBinding) -> tuple[SandboxMount, ...]:
    """Build deterministic virtual mounts for command execution."""
    mounts = [
        SandboxMount(source=binding.workspace, target=WORKSPACE_TARGET, read_only=True),
        SandboxMount(source=binding.scratch, target=SCRATCH_TARGET, read_only=False),
    ]
    for code, root in sorted(binding.skill_roots.items()):
        mounts.append(
            SandboxMount(
                source=root,
                target=SKILLS_TARGET / code,
                read_only=True,
            )
        )
    return tuple(mounts)


def decode_output(value: bytes) -> tuple[str, bool]:
    """Decode bounded process bytes and report whether replacement was needed."""
    try:
        return value.decode("utf-8"), False
    except UnicodeDecodeError:
        return value.decode("utf-8", errors="replace"), True


def command_output(result: SandboxResult, stdout: str, stderr: str, decoding_loss: bool) -> JsonObject:
    """Project a Sandbox result into the model-visible command output."""
    return {
        "exit_code": result.exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "decoding_loss": decoding_loss,
        "timed_out": result.timed_out,
        "output_truncated": result.output_truncated,
        "duration_seconds": result.duration_seconds,
        "backend": result.backend,
    }
