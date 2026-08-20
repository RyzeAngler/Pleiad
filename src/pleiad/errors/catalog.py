"""Default Chinese messages and recovery policy for Pleiad error codes."""

from typing import Any

from pleiad.errors.base import ErrorCode, ErrorInfo, ErrorKind

_MESSAGES: dict[ErrorCode, str] = {
    ErrorCode.AGENT_MODEL: "模型调用失败，无法继续进行下一轮决策",
    ErrorCode.AGENT_OUTPUT: "模型返回的最终结果不符合要求",
    ErrorCode.AGENT_CANCELLED: "智能体运行已被取消",
    ErrorCode.TOOL_UNKNOWN: "当前智能体没有可用的该工具",
    ErrorCode.TOOL_INVALID_ARGUMENTS: "工具参数不符合工具输入结构",
    ErrorCode.TOOL_PERMISSION: "工具调用缺少所需权限",
    ErrorCode.TOOL_TIMEOUT: "工具执行超时",
    ErrorCode.TOOL_OUTPUT: "工具返回结果不符合工具输出结构",
    ErrorCode.TOOL_EXECUTION: "工具执行失败",
    ErrorCode.GRAPH_INVALID: "任务图不符合运行要求",
    ErrorCode.GRAPH_CONFLICT: "任务图版本冲突，提交基于的版本已经过期",
    ErrorCode.GRAPH_EXECUTION: "任务图执行失败",
    ErrorCode.SYSTEM_CONFIGURATION: "系统配置无效，无法启动运行时",
    ErrorCode.SYSTEM_CAPABILITY: "能力加载失败，无法建立能力目录",
    ErrorCode.SYSTEM_CAPABILITY_RELOAD: "能力目录热加载失败，当前仍保留上一个可用版本",
    ErrorCode.SYSTEM_STORAGE: "运行状态持久化失败",
    ErrorCode.SYSTEM_UNEXPECTED: "系统发生未预期错误",
}

_POLICY: dict[ErrorCode, tuple[bool, bool]] = {
    ErrorCode.AGENT_MODEL: (False, True),
    ErrorCode.AGENT_OUTPUT: (False, True),
    ErrorCode.AGENT_CANCELLED: (False, True),
    ErrorCode.TOOL_UNKNOWN: (False, False),
    ErrorCode.TOOL_INVALID_ARGUMENTS: (True, False),
    ErrorCode.TOOL_PERMISSION: (False, True),
    ErrorCode.TOOL_TIMEOUT: (True, False),
    ErrorCode.TOOL_OUTPUT: (False, False),
    ErrorCode.TOOL_EXECUTION: (False, False),
    ErrorCode.GRAPH_INVALID: (False, True),
    ErrorCode.GRAPH_CONFLICT: (False, False),
    ErrorCode.GRAPH_EXECUTION: (False, True),
    ErrorCode.SYSTEM_CONFIGURATION: (False, True),
    ErrorCode.SYSTEM_CAPABILITY: (False, True),
    ErrorCode.SYSTEM_CAPABILITY_RELOAD: (True, False),
    ErrorCode.SYSTEM_STORAGE: (False, True),
    ErrorCode.SYSTEM_UNEXPECTED: (False, True),
}


def make_error(
    code: ErrorCode,
    *,
    details: dict[str, Any] | None = None,
    retryable: bool | None = None,
    terminal: bool | None = None,
) -> ErrorInfo:
    """Create one error with the default Chinese message and loop policy."""
    default_retryable, default_terminal = _POLICY[code]
    return ErrorInfo(
        code=code.value,
        message=_MESSAGES[code],
        kind=_kind(code),
        retryable=default_retryable if retryable is None else retryable,
        terminal=default_terminal if terminal is None else terminal,
        details=details or {},
    )


def _kind(code: ErrorCode) -> ErrorKind:
    """Map stable error code ownership to a broad category."""
    if code.value.startswith("agent."):
        return ErrorKind.AGENT
    if code.value.startswith("tool."):
        return ErrorKind.TOOL
    if code.value.startswith("graph."):
        return ErrorKind.GRAPH
    return ErrorKind.SYSTEM
