from enum import StrEnum

import pytest

from pleiad.errors.base import BusinessError, ErrorType, PleiadError, SystemFault


class ExampleErrorCode(StrEnum):
    FAILED = "example.operation.failed"
    BROKEN = "example.runtime.broken"


class ExampleBusinessError(BusinessError):
    def __init__(self, message: str) -> None:
        super().__init__(ExampleErrorCode.FAILED, message)


class ExampleSystemFault(SystemFault):
    def __init__(self, message: str) -> None:
        super().__init__(ExampleErrorCode.BROKEN, message)


def test_business_error_exposes_only_the_global_error_fields() -> None:
    error = ExampleBusinessError("The requested operation failed.")

    assert vars(error) == {
        "code": ExampleErrorCode.FAILED,
        "message": "The requested operation failed.",
        "type": ErrorType.BUSINESS,
    }
    assert str(error) == error.message


def test_business_error_uses_the_recoverable_control_flow_branch() -> None:
    error = ExampleBusinessError("The requested operation failed.")

    assert isinstance(error, PleiadError)
    assert isinstance(error, BusinessError)
    assert not isinstance(error, SystemFault)


def test_system_fault_uses_the_terminating_control_flow_branch() -> None:
    error = ExampleSystemFault("The runtime is unavailable.")

    assert error.type is ErrorType.SYSTEM
    assert isinstance(error, SystemFault)
    assert not isinstance(error, BusinessError)


@pytest.mark.parametrize(
    "code",
    [
        "invalid",
        "Example.failed",
        "example.operation-failed",
        "示例.失败",
    ],
)
def test_error_rejects_codes_outside_the_dotted_identifier_contract(code: str) -> None:
    with pytest.raises(ValueError, match="lowercase dotted identifiers"):
        BusinessError(code, "The operation failed.")


def test_error_rejects_an_empty_message() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        BusinessError("example.operation.failed", " ")
