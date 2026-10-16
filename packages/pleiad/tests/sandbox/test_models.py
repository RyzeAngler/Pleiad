import math
from pathlib import Path, PurePosixPath

import pytest
from pydantic import ValidationError

from pleiad.sandbox.models import SandboxMount, SandboxRequest, SandboxResult
from pleiad.sandbox.policy import SandboxPolicy


def policy(
    *,
    image: str = "python@sha256:" + "a" * 64,
    user: str = "65532:65532",
    allow_shell_command: bool = True,
) -> SandboxPolicy:
    """Build a valid policy for model-boundary tests."""
    return SandboxPolicy(image=image, user=user, allow_shell_command=allow_shell_command)


def test_policy_rejects_unpinned_images_and_root_users() -> None:
    with pytest.raises(ValidationError):
        policy(image="python:latest")
    with pytest.raises(ValidationError):
        policy(user="0:65532")
    with pytest.raises(ValidationError):
        policy(user="daemon:daemon")


def test_policy_rejects_removed_or_unimplemented_configuration() -> None:
    with pytest.raises(ValidationError):
        SandboxPolicy.model_validate({"image": "python@sha256:" + "a" * 64, "require_pinned_image": True})
    with pytest.raises(ValidationError):
        SandboxPolicy.model_validate({"image": "python@sha256:" + "a" * 64, "network": "controlled_proxy"})


def test_request_requires_policy_and_one_command_form(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="exactly one"):
        SandboxRequest(cwd=PurePosixPath("/pleiad"), policy=policy())
    with pytest.raises(ValueError, match="exactly one"):
        SandboxRequest(cwd=PurePosixPath("/pleiad"), policy=policy(), argv=("python",), shell_command="echo")
    with pytest.raises(ValueError, match="outside its policy"):
        SandboxRequest(
            cwd=PurePosixPath("/pleiad"),
            policy=policy(),
            argv=("python", "-c", "pass"),
            environment={"SECRET": "value"},
        )
    source = tmp_path / "workspace"
    source.mkdir()
    mount = SandboxMount(source, PurePosixPath("/pleiad/workspace"))
    assert mount.source == source.resolve()


def test_request_rejects_overlapping_mounts(tmp_path: Path) -> None:
    source = tmp_path / "workspace"
    source.mkdir()
    with pytest.raises(ValueError, match="must not overlap"):
        SandboxRequest(
            cwd=PurePosixPath("/pleiad"),
            policy=policy(),
            argv=("python",),
            mounts=(
                SandboxMount(source, PurePosixPath("/pleiad/workspace")),
                SandboxMount(source, PurePosixPath("/pleiad/workspace/nested")),
            ),
        )


def test_result_requires_actual_backend_and_finite_duration() -> None:
    with pytest.raises(ValueError, match="backend"):
        SandboxResult(exit_code=0)
    with pytest.raises(ValueError, match="finite"):
        SandboxResult(exit_code=0, backend="docker", duration_seconds=math.inf)
    with pytest.raises(ValueError, match="must not claim"):
        SandboxResult(exit_code=0, backend="docker", timed_out=True)
