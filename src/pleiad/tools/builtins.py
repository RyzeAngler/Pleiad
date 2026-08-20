"""Small built-in Tools used by Pleiad-generated Agents."""

import asyncio
from datetime import datetime
from pathlib import Path

from pleiad.contracts import JsonObject, JsonSchema
from pleiad.tools.base import BaseTool
from pleiad.tools.contracts import ToolContext, ToolDescriptor, ToolPolicy
from pleiad.tools.loader import ToolFactory
from pleiad.tools.zoo import ToolZoo


class _BuiltinTool(BaseTool):
    def __init__(self, descriptor: ToolDescriptor, root: Path) -> None:
        self._descriptor = descriptor
        self.root = root.resolve()

    @property
    def descriptor(self) -> ToolDescriptor:
        return self._descriptor

    @property
    def policy(self) -> ToolPolicy:
        return ToolPolicy(timeout_seconds=60, max_retries=0, max_concurrency=4)

    def _path(self, raw: str) -> Path:
        path = (self.root / raw).resolve()
        if path != self.root and self.root not in path.parents:
            raise PermissionError("path escapes the configured workspace")
        return path


class ReadTool(_BuiltinTool):
    @property
    def input_schema(self) -> JsonSchema:
        return {
            "type": "object",
            "properties": {"path": {"type": "string", "minLength": 1}},
            "required": ["path"],
            "additionalProperties": False,
        }

    async def execute(self, context: ToolContext, arguments: JsonObject) -> JsonObject:
        path = self._path(str(arguments["path"]))
        return {
            "path": str(path.relative_to(self.root)),
            "content": path.read_text(encoding="utf-8"),
        }


class WriteTool(_BuiltinTool):
    @property
    def input_schema(self) -> JsonSchema:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "minLength": 1},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        }

    async def execute(self, context: ToolContext, arguments: JsonObject) -> JsonObject:
        path = self._path(str(arguments["path"]))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(arguments["content"]), encoding="utf-8")
        return {"path": str(path.relative_to(self.root)), "bytes": path.stat().st_size}


class BashTool(_BuiltinTool):
    @property
    def input_schema(self) -> JsonSchema:
        return {
            "type": "object",
            "properties": {"command": {"type": "string", "minLength": 1}},
            "required": ["command"],
            "additionalProperties": False,
        }

    @property
    def policy(self) -> ToolPolicy:
        return ToolPolicy(timeout_seconds=30, max_retries=0, max_concurrency=2)

    async def execute(self, context: ToolContext, arguments: JsonObject) -> JsonObject:
        process = await asyncio.create_subprocess_exec(
            "/bin/bash",
            "-lc",
            str(arguments["command"]),
            cwd=self.root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        return {
            "exit_code": process.returncode or 0,
            "stdout": stdout.decode("utf-8", errors="replace"),
            "stderr": stderr.decode("utf-8", errors="replace"),
        }


class DateTimeTool(_BuiltinTool):
    """Return the current local date and time from the runtime host."""

    @property
    def input_schema(self) -> JsonSchema:
        return {"type": "object", "additionalProperties": False}

    async def execute(self, context: ToolContext, arguments: JsonObject) -> JsonObject:
        del context, arguments
        current = datetime.now().astimezone()
        return {
            "iso": current.isoformat(),
            "date": current.date().isoformat(),
            "time": current.strftime("%H:%M:%S"),
            "timezone": str(current.tzinfo),
            "unix_timestamp": int(current.timestamp()),
        }


def register_builtins(zoo: ToolZoo, root: Path) -> None:
    """Register Pleiad's minimal local Tool set."""
    descriptors = (
        ToolDescriptor(name="read", code="pleiad.tool.read", description="读取工作区中的文件内容"),
        ToolDescriptor(
            name="write", code="pleiad.tool.write", description="写入工作区中的文件内容"
        ),
        ToolDescriptor(
            name="bash",
            code="pleiad.tool.bash",
            description="在工作区中执行受控 Bash 命令",
        ),
        ToolDescriptor(
            name="get_datetime",
            code="pleiad.tool.datetime",
            description="获取当前日期、时间、时区和 Unix 时间戳",
        ),
    )
    zoo.register_builtin(descriptors[0], _read_factory(root))
    zoo.register_builtin(descriptors[1], _write_factory(root))
    zoo.register_builtin(descriptors[2], _bash_factory(root))
    zoo.register_builtin(descriptors[3], _datetime_factory(root))


def _read_factory(root: Path) -> ToolFactory:
    """Bind the read Tool to a workspace root."""

    def create(descriptor: ToolDescriptor) -> BaseTool:
        return ReadTool(descriptor, root)

    return create


def _write_factory(root: Path) -> ToolFactory:
    """Bind the write Tool to a workspace root."""

    def create(descriptor: ToolDescriptor) -> BaseTool:
        return WriteTool(descriptor, root)

    return create


def _bash_factory(root: Path) -> ToolFactory:
    """Bind the bash Tool to a workspace root."""

    def create(descriptor: ToolDescriptor) -> BaseTool:
        return BashTool(descriptor, root)

    return create


def _datetime_factory(root: Path) -> ToolFactory:
    """Bind the date-time Tool to the process workspace boundary."""

    def create(descriptor: ToolDescriptor) -> BaseTool:
        return DateTimeTool(descriptor, root)

    return create
