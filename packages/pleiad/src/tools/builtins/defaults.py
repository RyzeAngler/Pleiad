from collections.abc import Mapping
from pathlib import Path

from pleiad.sandbox.policy import SandboxPolicy
from pleiad.sandbox.sandbox import Sandbox
from pleiad.tools.base import BaseTool
from pleiad.tools.builtins.command import CommandTool
from pleiad.tools.builtins.file_support import FileToolBinding
from pleiad.tools.builtins.read_file import ReadFileTool
from pleiad.tools.builtins.write_file import WriteFileTool
from pleiad.tools.contracts import ToolDescriptor
from pleiad.tools.registry import ToolRegistry


def install_default_tools(
    registry: ToolRegistry,
    *,
    workspace: Path,
    scratch: Path,
    skill_roots: Mapping[str, Path] | None = None,
    sandbox: Sandbox,
    sandbox_policy: SandboxPolicy,
    excluded_workspace_paths: tuple[str, ...] = (),
    workspace_mask_root: Path | None = None,
) -> None:
    """Install the three model-visible SDK Tools into a host registry."""
    binding = FileToolBinding(
        workspace=workspace,
        scratch=scratch,
        skill_roots={} if skill_roots is None else skill_roots,
        excluded_workspace_paths=excluded_workspace_paths,
        workspace_mask_root=workspace_mask_root,
    )
    read_descriptor = ToolDescriptor(
        name="read_file",
        code="read_file",
        description="Read bounded UTF-8 text from workspace, selected skills, or scratch.",
    )
    write_descriptor = ToolDescriptor(
        name="write_file",
        code="write_file",
        description="Create or conditionally replace UTF-8 text in the workspace.",
    )
    command_descriptor = ToolDescriptor(
        name="command",
        code="command",
        description="Run a command inside the host-selected OCI Sandbox.",
    )

    def create_read_file(descriptor: ToolDescriptor) -> BaseTool:
        """Create a read_file Tool bound to the host roots."""
        return ReadFileTool(descriptor, binding)

    def create_write_file(descriptor: ToolDescriptor) -> BaseTool:
        """Create a write_file Tool bound to the host roots."""
        return WriteFileTool(descriptor, binding)

    def create_command(descriptor: ToolDescriptor) -> BaseTool:
        """Create a command Tool bound to the explicit Sandbox."""
        return CommandTool(descriptor, binding, sandbox, sandbox_policy)

    registry.register(read_descriptor, create_read_file)
    registry.register(write_descriptor, create_write_file)
    registry.register(command_descriptor, create_command)
