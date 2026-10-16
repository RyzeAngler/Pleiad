from pleiad.errors.base import ErrorType, SystemFault
from pleiad.models.errors import ModelErrorCode, ModelRateLimitError, classify_provider_error


class RateLimitFailure(Exception):
    status_code = 429


def test_specific_model_error_exposes_only_the_global_error_fields() -> None:
    error = ModelRateLimitError("The model provider request was rate limited.")

    assert vars(error) == {
        "code": ModelErrorCode.RATE_LIMIT,
        "message": "The model provider request was rate limited.",
        "type": ErrorType.SYSTEM,
    }
    assert isinstance(error, SystemFault)


def test_provider_error_classifies_rate_limit_without_exposing_provider_text() -> None:
    error = classify_provider_error(RateLimitFailure("secret provider response"))

    assert isinstance(error, ModelRateLimitError)
    assert error.code == ModelErrorCode.RATE_LIMIT
    assert error.message == "The model provider rate limit was exceeded."
