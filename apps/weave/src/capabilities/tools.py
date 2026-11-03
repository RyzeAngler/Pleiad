import json
from pathlib import Path, PurePosixPath

from jsonschema import Draft202012Validator, ValidationError
from pleiad.contracts import ContractModel, JsonObject, JsonSchema
from pleiad.sandbox.models import SandboxMount, SandboxRequest
from pleiad.sandbox.policy import SandboxPolicy
from pleiad.sandbox.sandbox import Sandbox
from pleiad.schemas import validate_json_schema
from pleiad.tools.base import BaseTool, ToolFailure, ToolOutput
from pleiad.tools.builtins.command import command_output, decode_output
from pleiad.tools.contracts import ToolContext, ToolDescriptor
from pleiad.tools.errors import ToolError
from pleiad.tools.registry import ToolRegistry
from pydantic import Field, TypeAdapter

JSON_OBJECT_ADAPTER: TypeAdapter[JsonObject] = TypeAdapter(JsonObject)


class ToolBinding(ContractModel):
    """Validate a host-authored Python script binding outside the public catalog."""

    script: str = Field(min_length=1)
    input_schema: JsonSchema
    output_schema: JsonSchema


class ScriptTool(BaseTool):
    """Send validated JSON to a workspace script exclusively through SDK Sandbox."""

    def __init__(
        self,
        descriptor: ToolDescriptor,
        binding: ToolBinding,
        workspace: Path,
        sandbox: Sandbox,
        sandbox_policy: SandboxPolicy,
    ) -> None:
        """Bind a manifest entry to the explicitly configured container policy."""
        self.tool_descriptor = descriptor
        self.binding = binding
        self.workspace = workspace
        self.sandbox = sandbox
        self.sandbox_policy = sandbox_policy

    @property
    def descriptor(self) -> ToolDescriptor:
        """Return the public catalog metadata."""
        return self.tool_descriptor

    @property
    def input_schema(self) -> JsonSchema:
        """Expose the script's validated JSON input contract."""
        return self.binding.input_schema

    @property
    def required_permissions(self) -> frozenset[str]:
        """Require the same host grant as the SDK command Tool."""
        return frozenset({"sandbox.command"})

    async def execute(self, context: ToolContext, arguments: JsonObject) -> ToolOutput:
        """Run the selected script without importing user code into the host."""
        result = await self.sandbox.execute(
            SandboxRequest(
                argv=("python", f"/workspace/tools/{self.binding.script}"),
                cwd=PurePosixPath("/workspace"),
                mounts=(SandboxMount(self.workspace, PurePosixPath("/workspace"), read_only=False),),
                stdin=json.dumps(arguments, ensure_ascii=False).encode("utf-8"),
                policy=self.sandbox_policy,
            )
        )
        if result.timed_out or result.exit_code != 0 or result.output_truncated:
            stdout, stdout_loss = decode_output(result.stdout)
            stderr, stderr_loss = decode_output(result.stderr)
            return ToolFailure(
                error=ToolError(
                    "weave.workspace_tool_failed", "Workspace script did not return a complete successful result."
                ),
                output=command_output(result, stdout, stderr, stdout_loss or stderr_loss),
            )
        try:
            output = JSON_OBJECT_ADAPTER.validate_json(result.stdout)
            Draft202012Validator(self.binding.output_schema).validate(output)
        except ValidationError as error:
            raise ToolError(
                "weave.workspace_tool_output", "Workspace script output does not satisfy its JSON Schema."
            ) from error
        except (ValueError, TypeError) as error:
            raise ToolError("weave.workspace_tool_output", "Workspace script returned invalid JSON.") from error
        return output


def install_tools(
    registry: ToolRegistry,
    descriptors: tuple[ToolDescriptor, ...],
    workspace: Path,
    sandbox: Sandbox,
    policy: SandboxPolicy,
) -> None:
    """Register validated script contracts while leaving their code in the container."""
    for descriptor in descriptors:
        if registry.contains(descriptor.code):
            raise ValueError(f"Workspace Tool shadows a built-in: {descriptor.code}")
        if not descriptor.name.isidentifier():
            raise ValueError("Workspace Tool name must be a Python identifier")
        path = workspace / "tools" / f"{descriptor.name}.json"
        binding = ToolBinding.model_validate_json(path.read_bytes())
        script = workspace / "tools" / binding.script
        relative = PurePosixPath(binding.script)
        if relative.is_absolute() or ".." in relative.parts or script.is_symlink():
            raise ValueError("Workspace Tool script must stay inside tools/")
        if not script.is_file() or not script.resolve().is_relative_to((workspace / "tools").resolve()):
            raise ValueError("Workspace Tool script is missing or outside tools/")
        compile(script.read_text(encoding="utf-8"), str(script), "exec")
        validate_json_schema(binding.input_schema)
        validate_json_schema(binding.output_schema)

        def create(item: ToolDescriptor, selected: ToolBinding = binding) -> BaseTool:
            """Bind this validated script to a fresh SDK Tool instance."""
            return ScriptTool(item, selected, workspace, sandbox, policy)

        registry.register(descriptor, create)
