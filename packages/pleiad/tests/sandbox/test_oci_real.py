import asyncio
import os
from pathlib import Path, PurePosixPath

import pytest

from pleiad.sandbox.backends.oci import OciSandbox
from pleiad.sandbox.models import SandboxMount, SandboxRequest
from pleiad.sandbox.policy import SandboxPolicy
from pleiad.sandbox.sandbox import Sandbox


@pytest.mark.oci
@pytest.mark.parametrize(
    ("runtime", "image_environment"),
    [("docker", "PLEIAD_DOCKER_SANDBOX_IMAGE"), ("podman", "PLEIAD_PODMAN_SANDBOX_IMAGE")],
    ids=("docker", "podman"),
)
def test_real_oci_preserves_command_facts_and_mount_permissions(
    runtime: str,
    image_environment: str,
    tmp_path: Path,
) -> None:
    image = os.environ.get(image_environment)
    if image is None:
        pytest.skip(f"set {image_environment} to a pre-pulled sha256 image digest")
    workspace = tmp_path / "workspace"
    scratch = tmp_path / "scratch"
    workspace.mkdir()
    scratch.mkdir()
    policy = SandboxPolicy(
        image=image,
        allow_shell_command=True,
        max_output_bytes=64,
    )
    request = SandboxRequest(
        cwd=PurePosixPath("/pleiad"),
        policy=policy,
        shell_command=(
            'if [ "$(id -u)" = "0" ]; then exit 8; fi; '
            "if printf forbidden > /pleiad/workspace/blocked; then exit 9; fi; "
            "printf generated > /pleiad/scratch/generated.txt; "
            "printf out; printf err >&2; exit 7"
        ),
        mounts=(
            SandboxMount(workspace, PurePosixPath("/pleiad/workspace"), read_only=True),
            SandboxMount(scratch, PurePosixPath("/pleiad/scratch"), read_only=False),
        ),
    )

    result = asyncio.run(Sandbox(OciSandbox(runtime)).execute(request))

    assert result.exit_code == 7
    assert result.stdout == b"out"
    assert result.stderr == b"err"
    assert result.timed_out is False
    assert not (workspace / "blocked").exists()
    assert (scratch / "generated.txt").read_text(encoding="utf-8") == "generated"
