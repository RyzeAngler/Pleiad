import pytest

from pleiad.observability.privacy import PrivacyPolicy


def test_privacy_policy_denies_prompt_and_command_attributes_by_default() -> None:
    policy = PrivacyPolicy()

    attributes = policy.sanitize_attributes(
        {
            "prompt": "secret prompt",
            "command": "rm -rf /",
            "gen_ai.request.model": "model-name",
            "pleiad.run.id": "run-1",
        }
    )

    assert attributes == {"gen_ai.request.model": "model-name", "pleiad.run.id": "run-1"}


def test_privacy_policy_masks_and_limits_explicit_content() -> None:
    policy = PrivacyPolicy(
        capture_inputs=True,
        max_content_bytes=5,
        masker=lambda value: value.replace("secret", "masked"),
    )

    captured = policy.capture_text("secret payload", field="input")

    assert captured is not None
    assert captured.value == "maske"
    assert captured.original_bytes == len(b"masked payload")
    assert captured.truncated is True


def test_privacy_policy_drops_content_when_masker_fails(caplog: pytest.LogCaptureFixture) -> None:
    def broken_masker(value: str) -> str:
        raise RuntimeError(value)

    policy = PrivacyPolicy(capture_outputs=True, masker=broken_masker)

    assert policy.capture_text("secret output", field="output") is None
    assert "secret output" not in caplog.text
    assert "content was discarded" in caplog.text


def test_privacy_policy_uses_an_attribute_allowlist() -> None:
    """Unknown metadata and sensitive transport values are denied by default."""
    policy = PrivacyPolicy()

    attributes = policy.sanitize_attributes(
        {
            "authorization": "Bearer synthetic-secret",
            "http.request.header.authorization": "synthetic-secret",
            "url.full": "https://synthetic-secret@example.test/path",
            "exception.message": "synthetic-secret",
            "custom.metadata": "synthetic-secret",
            "pleiad.status": "failed",
        }
    )

    assert attributes == {"pleiad.status": "failed"}


def test_privacy_policy_bounds_array_count_items_and_total_size() -> None:
    """Allowed attributes cannot bypass collection and aggregate byte limits."""
    policy = PrivacyPolicy(
        max_attribute_bytes=3,
        max_attributes=2,
        max_array_items=2,
        max_total_attribute_bytes=64,
    )

    attributes = policy.sanitize_attributes(
        {
            "gen_ai.response.finish_reasons": ("lengthy", "stop", "ignored"),
            "pleiad.status": "succeeded",
            "pleiad.error.code": "must-not-fit-count",
        }
    )

    assert attributes == {
        "gen_ai.response.finish_reasons": ("len", "sto"),
        "pleiad.status": "suc",
    }


def test_privacy_policy_truncates_to_a_valid_utf8_prefix() -> None:
    """Byte limiting never skips an oversized character to reveal later text."""
    policy = PrivacyPolicy(capture_inputs=True, max_content_bytes=3)

    captured = policy.capture_text("四secret", field="input")

    assert captured is not None
    assert captured.value == "四"
    assert captured.original_bytes == len("四secret".encode())
    assert captured.truncated is True
