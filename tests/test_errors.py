"""Central error vocabulary tests."""

from pleiad.errors import ErrorCode, ErrorKind, make_error


def test_errors_have_chinese_message_and_terminal_policy() -> None:
    """The loop uses catalogued policy instead of exception text."""
    recoverable = make_error(ErrorCode.TOOL_INVALID_ARGUMENTS)
    terminal = make_error(ErrorCode.SYSTEM_UNEXPECTED)
    assert recoverable.kind is ErrorKind.TOOL
    assert recoverable.message == "工具参数不符合工具输入结构"
    assert not recoverable.terminal
    assert terminal.terminal
    assert terminal.code == "system.unexpected_error"
