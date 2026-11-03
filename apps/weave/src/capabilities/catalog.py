import asyncio
from dataclasses import dataclass
from pathlib import Path

from pleiad.sandbox.policy import SandboxPolicy
from pleiad.sandbox.sandbox import Sandbox
from pleiad.skills.loader import SkillLoader
from pleiad.skills.manifest import load_skill_descriptors
from pleiad.skills.package import SkillPackage
from pleiad.tools.builtins.defaults import install_default_tools
from pleiad.tools.manifest import load_tool_descriptors
from pleiad.tools.registry import ToolRegistry

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
    return await asyncio.to_thread(load_capabilities, workspace, manifest, sandbox, policy)
