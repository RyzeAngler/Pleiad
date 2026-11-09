import logging
from pathlib import PurePosixPath

from pydantic import ConfigDict, Field, field_validator

from pleiad.contracts import ContractModel, JsonObject, JsonSchema
from pleiad.sandbox.errors import SandboxCancelledError, SandboxUnavailableError
from pleiad.sandbox.models import SandboxMount, SandboxReceipt, SandboxRequest, SandboxResult
from pleiad.sandbox.policy import SandboxPolicy
from pleiad.sandbox.sandbox import Sandbox
from pleiad.tools.base import BackgroundAdmission, BaseTool, ToolOutput
from pleiad.tools.builtins.file_support import FileToolBinding, validate_root
from pleiad.tools.contracts import ToolAcceptance, ToolContext, ToolDescriptor, ToolPolicy, ToolSubmission
from pleiad.tools.errors import BackgroundNotAcceptedError, ToolCancelledError

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
        if binding.excluded_workspace_paths and binding.workspace_mask_root is None:
            raise ValueError("command Tool workspace exclusions require an empty host-owned mask directory")
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
        return ToolPolicy(
            timeout_seconds=self.sandbox_policy.timeout_seconds,
            max_concurrency=1,
            background=True,
        )

    @property
    def supports_background(self) -> bool:
        """Expose the concrete OCI create receipt as the command admission boundary."""
        return True

    def request(self, arguments: JsonObject, *, operation_id: str | None = None) -> SandboxRequest:
        """Resolve command arguments and host mounts into one validated backend request."""
        parsed = CommandArguments.model_validate(arguments)
        return SandboxRequest(
            cwd=COMMAND_CWD,
            mounts=build_mounts(self.binding),
            policy=self.sandbox_policy,
            shell_command=parsed.command,
            operation_id=operation_id,
        )

    async def execute(self, context: ToolContext, arguments: JsonObject) -> ToolOutput:
        """Execute command text with workspace and read-only roots mounted."""
        result = await self.sandbox.execute(self.request(arguments))
        return self.output(context, result)

    async def execute_background(
        self, context: ToolContext, arguments: JsonObject, admission: BackgroundAdmission
    ) -> ToolOutput:
        """Acknowledge the actual created OCI container before it starts running."""

        async def accepted(receipt: SandboxReceipt) -> None:
            await admission.accept(
                ToolAcceptance(
                    operation_id=admission.operation_id,
                    backend=receipt.runtime,
                    reference=receipt.container_id,
                    request_digest=receipt.request_digest,
                    details=receipt.model_dump(mode="json"),
                )
            )

        request = self.request(arguments, operation_id=admission.operation_id)
        await admission.prepare(ToolSubmission(operation_id=admission.operation_id, request_digest=request.digest()))
        try:
            result = await self.sandbox.execute(request, on_accept=accepted)
        except SandboxCancelledError as error:
            raise ToolCancelledError(confirmed_stop=error.confirmed_stop) from error
        except SandboxUnavailableError as error:
            if admission.receipt is not None:
                raise
            raise BackgroundNotAcceptedError(error.message) from error
        return self.output(context, result)

    def output(self, context: ToolContext, result: SandboxResult) -> JsonObject:
        """Project actual bounded backend output with diagnostic execution metadata."""
        stdout, stdout_loss = decode_output(result.stdout)
        stderr, stderr_loss = decode_output(result.stderr)
        logger.info(
            f"command completed call_id={context.call_id} backend={result.backend} exit_code={result.exit_code} "
            f"timed_out={result.timed_out} output_truncated={result.output_truncated}"
        )
        return command_output(result, stdout, stderr, stdout_loss or stderr_loss)


def build_mounts(binding: FileToolBinding) -> tuple[SandboxMount, ...]:
    """Build deterministic virtual mounts for command execution."""
    mounts = [
        *workspace_mounts(binding, WORKSPACE_TARGET, read_only=True),
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


def workspace_mounts(binding: FileToolBinding, target: PurePosixPath, *, read_only: bool) -> tuple[SandboxMount, ...]:
    """Bind a workspace view with host-owned subtrees masked from scripts.

    Args:
        binding: Validated host roots and workspace exclusions.
        target: The enclosing workspace directory in the sandbox.
        read_only: Whether ordinary workspace files are writable in this view.

    Returns:
        The workspace mount followed by its restrictive directory masks.

    Raises:
        ValueError: If an exclusion cannot be safely masked.
    """
    mounts = [SandboxMount(source=binding.workspace, target=target, read_only=read_only)]
    for relative_path in binding.excluded_workspace_paths:
        excluded_root = binding.workspace / relative_path
        if validate_root(excluded_root, "excluded workspace") != excluded_root:
            raise ValueError("Excluded workspace directories must not contain symlinks")
        mask_root = binding.workspace_mask_root
        if mask_root is None:
            raise ValueError("command Tool workspace exclusions require a host-owned mask directory")
        mounts.append(SandboxMount(source=mask_root, target=target / relative_path, read_only=True, mask=True))
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
