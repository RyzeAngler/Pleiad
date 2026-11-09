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


@pytest.mark.parametrize("workspace_read_only", [False, True])
def test_request_accepts_an_empty_read_only_mask_beneath_an_explicit_workspace_mount(
    tmp_path: Path, workspace_read_only: bool
) -> None:
    workspace = tmp_path / "workspace"
    mask = tmp_path / "mask"
    workspace.mkdir()
    mask.mkdir()
    request = SandboxRequest(
        cwd=PurePosixPath("/pleiad"),
        policy=policy(),
        argv=("python",),
        mounts=(
            SandboxMount(workspace, PurePosixPath("/pleiad/workspace"), read_only=workspace_read_only),
            SandboxMount(mask, PurePosixPath("/pleiad/workspace/owner-state"), read_only=True, mask=True),
        ),
    )

    assert request.mounts[0].read_only is workspace_read_only
    assert request.mounts[1].mask and request.mounts[1].read_only
    assert request.mounts[1].source == mask


@pytest.mark.parametrize(
    ("invalid", "message"),
    [
        ("missing_parent", "masks require"),
        ("parent_after_mask", "masks require"),
        ("nested_masks", "must not overlap"),
        ("populated_source", "masks require"),
    ],
)
def test_request_rejects_masks_that_cannot_hide_the_host_owned_subtree(
    tmp_path: Path, invalid: str, message: str
) -> None:
    workspace = tmp_path / "workspace"
    mask = tmp_path / "mask"
    workspace.mkdir()
    mask.mkdir()
    parent = SandboxMount(workspace, PurePosixPath("/pleiad/workspace"), read_only=False)
    child = SandboxMount(mask, PurePosixPath("/pleiad/workspace/owner-state"), mask=True)
    mounts: tuple[SandboxMount, ...] = (parent, child)
    if invalid == "missing_parent":
        mounts = (child,)
    elif invalid == "parent_after_mask":
        mounts = (child, parent)
    elif invalid == "nested_masks":
        mounts = (parent, child, SandboxMount(mask, child.target / "nested", mask=True))
    else:
        (mask / "leaked.txt").write_text("must not be mounted", encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        SandboxRequest(cwd=PurePosixPath("/pleiad"), policy=policy(), argv=("python",), mounts=mounts)


def test_mask_mount_rejects_writable_or_nonempty_sources(tmp_path: Path) -> None:
    mask = tmp_path / "mask"
    mask.mkdir()
    with pytest.raises(ValueError, match="mounted read-only"):
        SandboxMount(mask, PurePosixPath("/pleiad/workspace/private"), read_only=False, mask=True)
    (mask / "data").write_bytes(b"private")
    with pytest.raises(ValueError, match="empty real directory"):
        SandboxMount(mask, PurePosixPath("/pleiad/workspace/private"), mask=True)


def test_result_requires_actual_backend_and_finite_duration() -> None:
    with pytest.raises(ValueError, match="backend"):
        SandboxResult(exit_code=0)
    with pytest.raises(ValueError, match="finite"):
        SandboxResult(exit_code=0, backend="docker", duration_seconds=math.inf)
    with pytest.raises(ValueError, match="must not claim"):
        SandboxResult(exit_code=0, backend="docker", timed_out=True)
