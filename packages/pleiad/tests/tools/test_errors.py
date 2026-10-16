from datetime import UTC, datetime

import pytest

from pleiad.errors.base import BusinessError, ErrorType
from pleiad.tools.contracts import ToolResult
from pleiad.tools.errors import (
    InvalidToolArgumentsError,
    InvalidToolOutputError,
    ToolError,
    ToolErrorCode,
    ToolExecutionFailedError,
    ToolPermissionDeniedError,
    ToolSystemFault,
    ToolTimeoutError,
    UnknownToolError,
)


def test_tool_error_uses_the_business_error_control_flow() -> None:
    error = ToolExecutionFailedError()

    assert isinstance(error, ToolError)
    assert isinstance(error, BusinessError)
    assert error.type is ErrorType.BUSINESS


@pytest.mark.parametrize(
    ("error", "expected_code"),
    [
        (UnknownToolError(), ToolErrorCode.NOT_FOUND),
        (InvalidToolArgumentsError(), ToolErrorCode.INVALID_ARGUMENTS),
        (ToolPermissionDeniedError(), ToolErrorCode.PERMISSION_DENIED),
        (ToolTimeoutError(), ToolErrorCode.TIMEOUT),
        (InvalidToolOutputError(), ToolErrorCode.INVALID_OUTPUT),
    ],
)
def test_tool_error_classes_expose_their_component_code(error: ToolError, expected_code: ToolErrorCode) -> None:
    assert error.code == expected_code


def test_failed_tool_result_records_three_flat_error_fields() -> None:
    now = datetime.now(UTC)
    error = UnknownToolError("The requested Tool is not available: lookup.")

    result = ToolResult(
        call_id="call-1",
        ok=False,
        error_code=error.code,
        error_message=error.message,
        error_type=error.type,
        started_at=now,
        finished_at=now,
    )

    assert result.error_code == ToolErrorCode.NOT_FOUND
    assert result.error_message == "The requested Tool is not available: lookup."
    assert result.error_type is ErrorType.BUSINESS


def test_tool_result_rejects_partial_error_fields() -> None:
    now = datetime.now(UTC)

    with pytest.raises(ValueError, match="provided together"):
        ToolResult(
            call_id="call-1",
            ok=False,
            error_code=ToolErrorCode.NOT_FOUND,
            started_at=now,
            finished_at=now,
        )


def test_tool_result_rejects_a_system_fault() -> None:
    now = datetime.now(UTC)
    error = ToolSystemFault()

    with pytest.raises(ValueError, match="business errors"):
        ToolResult(
            call_id="call-1",
            ok=False,
            error_code=error.code,
            error_message=error.message,
            error_type=error.type,
            started_at=now,
            finished_at=now,
        )
