import asyncio
from dataclasses import dataclass
from pathlib import Path

from fastmcp.client.transports import ClientTransport
from pleiad.mcp.discovery import discover_tools
from pleiad.mcp.tool import McpTool
from pleiad.sandbox.policy import SandboxPolicy
from pleiad.sandbox.sandbox import Sandbox
from pleiad.skills.loader import SkillLoader
from pleiad.skills.manifest import load_skill_descriptors
from pleiad.skills.package import SkillPackage
from pleiad.tools.base import BaseTool
from pleiad.tools.builtins.defaults import install_default_tools
from pleiad.tools.contracts import ToolDescriptor, ToolPolicy
from pleiad.tools.manifest import load_tool_descriptors
from pleiad.tools.registry import ToolRegistry

from pleiad_weave.capabilities.mcp import create_mcp_server, load_mcp_configuration
from pleiad_weave.capabilities.tools import install_tools


@dataclass(frozen=True, slots=True)
class CapabilitySnapshot:
    """Keep one execution's Tool and Skill bindings stable while files can change."""

    tools: ToolRegistry
    skills: SkillLoader


def load_capabilities(workspace: Path, manifest: Path, sandbox: Sandbox, policy: SandboxPolicy) -> CapabilitySnapshot:
    """Build fresh SDK bindings at a request boundary without importing user code."""
    skills = SkillLoader()
    skill_roots: dict[str, Path] = {}
    for descriptor in load_skill_descriptors(manifest):
        package_root = workspace / "skills" / descriptor.code
        if not package_root.resolve().is_relative_to((workspace / "skills").resolve()):
            raise ValueError("Workspace Skill package must stay inside skills/; symlink escapes are not allowed")
        package = SkillPackage(package_root.absolute())
        package.load(descriptor)
        skills.register(descriptor, package)
        skill_roots[descriptor.code] = package.root
    scratch = workspace / "runtime" / "scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    tools = ToolRegistry()
    install_default_tools(
        tools, workspace=workspace, scratch=scratch, skill_roots=skill_roots, sandbox=sandbox, sandbox_policy=policy
    )
    install_tools(tools, load_tool_descriptors(manifest), workspace, sandbox, policy)
    return CapabilitySnapshot(tools, skills)


async def refresh_capabilities(
    workspace: Path, manifest: Path, sandbox: Sandbox, policy: SandboxPolicy
) -> CapabilitySnapshot:
    """Read the current workspace at each Execution boundary off the event loop."""
    snapshot = await asyncio.to_thread(load_capabilities, workspace, manifest, sandbox, policy)
    configuration = await asyncio.to_thread(load_mcp_configuration, workspace)
    for connection in configuration.servers:
        if not connection.enabled:
            continue
        server = await asyncio.to_thread(create_mcp_server, connection, workspace)
        discovered_tools = await discover_tools(server)
        allowed_tools = None if connection.allowed_tools is None else set(connection.allowed_tools)
        if allowed_tools is not None:
            unknown = allowed_tools - {tool.info.remote_name for tool in discovered_tools}
            if unknown:
                raise ValueError(
                    f"MCP server {connection.code} allowed_tools names unknown remote tools: "
                    f"{', '.join(sorted(unknown))}"
                )
        for discovered in discovered_tools:
            if allowed_tools is not None and discovered.info.remote_name not in allowed_tools:
                continue
            tool = McpTool(server, discovered.info, policy=ToolPolicy(timeout_seconds=connection.timeout_seconds))

            def create(descriptor: ToolDescriptor, selected: McpTool[ClientTransport] = tool) -> BaseTool:
                """Bind one discovered SDK Tool without replacing its execution."""
                return selected

            snapshot.tools.register(tool.descriptor, create)
    return snapshot
