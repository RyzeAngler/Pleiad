"""Workspace Tool registration entrypoint loaded by Pleiad at process startup."""

from pathlib import Path

from pleiad.tools import BaseTool, ToolDescriptor, ToolZoo

from .text_statistics import TextStatisticsTool


def register_tools(zoo: ToolZoo, workspace: Path) -> None:
    """Bind stable Tool codes to workspace-owned implementation factories."""

    def create_text_statistics(descriptor: ToolDescriptor) -> BaseTool:
        return TextStatisticsTool(descriptor, workspace)

    zoo.register_factory("example.tool.text_statistics", create_text_statistics)
