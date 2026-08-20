"""Example custom Tool that calculates deterministic text-file statistics."""

from pathlib import Path

from pleiad.contracts import JsonObject, JsonSchema
from pleiad.tools import BaseTool, ToolContext, ToolDescriptor


class TextStatisticsTool(BaseTool):
    """Count lines, words, and characters in a workspace text file."""

    def __init__(self, descriptor: ToolDescriptor, workspace: Path) -> None:
        """Bind YAML metadata and the allowed workspace root."""
        self._descriptor = descriptor
        self._workspace = workspace.resolve()

    @property
    def descriptor(self) -> ToolDescriptor:
        """Return YAML-owned display metadata and stable code."""
        return self._descriptor

    @property
    def input_schema(self) -> JsonSchema:
        """Describe the relative text-file path accepted by this Tool."""
        return {
            "type": "object",
            "properties": {"path": {"type": "string", "minLength": 1}},
            "required": ["path"],
            "additionalProperties": False,
        }

    @property
    def output_schema(self) -> JsonSchema:
        """Describe the deterministic statistics returned to an Agent."""
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "lines": {"type": "integer", "minimum": 0},
                "non_empty_lines": {"type": "integer", "minimum": 0},
                "words": {"type": "integer", "minimum": 0},
                "characters": {"type": "integer", "minimum": 0},
            },
            "required": ["path", "lines", "non_empty_lines", "words", "characters"],
            "additionalProperties": False,
        }

    async def execute(self, context: ToolContext, arguments: JsonObject) -> JsonObject:
        """Read one safe workspace path and return its text statistics."""
        del context
        path = self._resolve_path(str(arguments["path"]))
        content = path.read_text(encoding="utf-8")
        lines = content.splitlines()
        return {
            "path": str(path.relative_to(self._workspace)),
            "lines": len(lines),
            "non_empty_lines": sum(bool(line.strip()) for line in lines),
            "words": len(content.split()),
            "characters": len(content),
        }

    def _resolve_path(self, raw_path: str) -> Path:
        """Keep the example Tool inside its configured workspace boundary."""
        path = (self._workspace / raw_path).resolve()
        if path != self._workspace and self._workspace not in path.parents:
            raise PermissionError("path escapes the configured workspace")
        return path
