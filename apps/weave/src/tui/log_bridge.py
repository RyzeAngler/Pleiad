import logging
import re
from collections.abc import Callable
from datetime import datetime

from pleiad_weave.infrastructure.logging import redact_log_credentials
from pleiad_weave.tui.state import LogRecordView

MAX_LOG_MESSAGE_CHARS = 280


class TuiLogHandler(logging.Handler):
    """Forward safe, compact application log records to the Textual loop."""

    def __init__(self, callback: Callable[[LogRecordView], None]) -> None:
        super().__init__(level=logging.INFO)
        self.callback = callback

    def emit(self, record: logging.LogRecord) -> None:
        """Format one record without blocking the application's log queue."""
        try:
            message = summarize_log_message(safe_log_message(record.getMessage()))
            self.callback(
                LogRecordView(
                    record_id=f"{record.created:.6f}:{id(record)}",
                    timestamp=datetime.fromtimestamp(record.created).astimezone(),
                    level=record.levelname,
                    message=message,
                    logger=record.name,
                    execution_id=record_field(record, "execution_id"),
                    turn_id=record_field(record, "turn_id"),
                    tool_code=record_field(record, "tool_code"),
                )
            )
        except Exception:
            self.handleError(record)


def record_field(record: logging.LogRecord, name: str) -> str | None:
    """Read one safe correlation field from a LogRecord extra mapping."""
    value = record.__dict__.get(name)
    return value if isinstance(value, str) and value else None


def summarize_log_message(message: str) -> str:
    """Explain known SDK operations without changing unknown diagnostic messages."""
    started = re.fullmatch(r"run started run=\S+ agent=(.+)", message)
    if started is not None:
        agent = started.group(1)
        return "主助手开始处理任务" if agent == "weave-supervisor" else f"智能体开始运行：{agent}"
    ended = re.fullmatch(r"run ended run=\S+ status=(\S+)", message)
    if ended is not None:
        status = ended.group(1)
        return {
            "succeeded": "智能体运行完成",
            "failed": "智能体运行失败",
            "cancelled": "智能体运行已取消",
            "timed_out": "智能体运行超时",
            "interrupted": "智能体运行已暂停",
            "limit_reached": "智能体运行达到限额",
        }.get(status, f"智能体结束运行（{status}）")
    file_operation = re.fullmatch(
        r"file (read|write) call_id=\S+ (?:scope=\S+ )?path=(.+) bytes=(\d+)(?: .*)?", message
    )
    if file_operation is not None:
        operation, path, size = file_operation.groups()
        action = "读取文件" if operation == "read" else "写入文件"
        truncated = "；内容已截断" if message.endswith("truncated=True") else ""
        return f"{action}：{path}（{size} 字节）{truncated}"
    command = re.fullmatch(
        r"command completed call_id=\S+ backend=(\S+) exit_code=(None|-?\d+) "
        r"timed_out=(True|False) output_truncated=(True|False)",
        message,
    )
    if command is not None:
        backend, exit_code, timed_out, truncated = command.groups()
        if timed_out == "True":
            summary = "命令执行超时"
        elif exit_code == "0":
            summary = f"命令执行成功（{backend}）"
        elif exit_code == "None":
            summary = f"命令未正常结束（{backend}）"
        else:
            summary = f"命令执行失败（退出码 {exit_code}）"
        return f"{summary}；输出已截断" if truncated == "True" else summary
    failed_tool = re.fullmatch(r"tool business failure call_id=\S+ code=(\S+)", message)
    if failed_tool is not None:
        return f"工具执行失败：{failed_tool.group(1)}"
    if message.startswith("Execution task failed execution_id="):
        return "任务执行失败，详细原因见 weave.log"
    if message.startswith("Execution failed during shutdown execution_id="):
        return "任务关闭时发生错误，详细原因见 weave.log"
    return message


def safe_log_message(message: str) -> str:
    """Redact obvious credential assignments and bound the visible line length."""
    compact = " ".join(redact_log_credentials(message).split())
    if len(compact) <= MAX_LOG_MESSAGE_CHARS:
        return compact
    return compact[: MAX_LOG_MESSAGE_CHARS - 1] + "…"
