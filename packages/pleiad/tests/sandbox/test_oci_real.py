import asyncio
import os
from pathlib import Path, PurePosixPath
from uuid import uuid4

import pytest

from pleiad.sandbox.backends.oci import OciSandbox
from pleiad.sandbox.errors import SandboxCancelledError, SandboxExecutionError
from pleiad.sandbox.models import SandboxMount, SandboxReceipt, SandboxRequest
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
            "if { printf forbidden > /pleiad/workspace/blocked; } 2>/dev/null; then exit 9; fi; "
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


@pytest.mark.oci
@pytest.mark.parametrize(
    ("runtime", "image_environment"),
    [("docker", "PLEIAD_DOCKER_SANDBOX_IMAGE"), ("podman", "PLEIAD_PODMAN_SANDBOX_IMAGE")],
    ids=("docker", "podman"),
)
@pytest.mark.parametrize("payload", [b'{"value":"input"}\n\x00\xff', b""], ids=("binary", "empty"))
def test_real_oci_delivers_stdin_bytes_and_eof_to_the_container(
    runtime: str, image_environment: str, payload: bytes
) -> None:
    image = os.environ.get(image_environment)
    if image is None:
        pytest.skip(f"set {image_environment} to a pre-pulled sha256 image digest")
    request = SandboxRequest(
        cwd=PurePosixPath("/"),
        policy=SandboxPolicy(image=image, max_output_bytes=64),
        argv=("python", "-c", "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"),
        stdin=payload,
    )

    result = asyncio.run(Sandbox(OciSandbox(runtime)).execute(request))

    assert result.exit_code == 0
    assert result.stdout == payload
    assert result.stderr == b""
    assert result.timed_out is False


@pytest.mark.oci
@pytest.mark.parametrize(
    ("runtime", "image_environment"),
    [("docker", "PLEIAD_DOCKER_SANDBOX_IMAGE"), ("podman", "PLEIAD_PODMAN_SANDBOX_IMAGE")],
    ids=("docker", "podman"),
)
def test_real_oci_masks_private_state_beneath_a_writable_workspace(
    runtime: str, image_environment: str, tmp_path: Path
) -> None:
    image = os.environ.get(image_environment)
    if image is None:
        pytest.skip(f"set {image_environment} to a pre-pulled sha256 image digest")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.chmod(0o777)  # The container's unprivileged user must be able to write this disposable fixture.
    private = workspace / "owner-state"
    private.mkdir()
    (private / "secret.txt").write_text("private host state", encoding="utf-8")
    mask = tmp_path / "mask"
    mask.mkdir()
    mask.chmod(0o777)  # The mount must prevent writes independently of source permissions.
    script = (
        "import errno\n"
        "from pathlib import Path\n"
        "hidden = Path('/workspace/owner-state')\n"
        "assert list(hidden.iterdir()) == []\n"
        "try:\n"
        "    (hidden / 'changed').write_text('forbidden')\n"
        "except OSError as error:\n"
        "    assert error.errno == errno.EROFS\n"
        "else:\n"
        "    raise SystemExit('masked state was writable')\n"
        "Path('/workspace/result.txt').write_text('allowed')\n"
    )
    request = SandboxRequest(
        cwd=PurePosixPath("/workspace"),
        policy=SandboxPolicy(image=image),
        argv=("python", "-c", script),
        mounts=(
            SandboxMount(workspace, PurePosixPath("/workspace"), read_only=False),
            SandboxMount(mask, PurePosixPath("/workspace/owner-state"), read_only=True, mask=True),
        ),
    )

    result = asyncio.run(Sandbox(OciSandbox(runtime)).execute(request))

    assert result.exit_code == 0, result.stderr
    assert (workspace / "result.txt").read_text(encoding="utf-8") == "allowed"
    assert (private / "secret.txt").read_text(encoding="utf-8") == "private host state"
    assert tuple(mask.iterdir()) == ()


@pytest.mark.oci
@pytest.mark.parametrize(
    ("runtime", "image_environment"),
    [("docker", "PLEIAD_DOCKER_SANDBOX_IMAGE"), ("podman", "PLEIAD_PODMAN_SANDBOX_IMAGE")],
    ids=("docker", "podman"),
)
def test_real_oci_admission_can_be_queried_and_stopped_without_duplicate_cleanup_adopting_it(
    runtime: str, image_environment: str
) -> None:
    """Actual daemon acceptance survives an SDK instance change and conflicting retry."""
    image = os.environ.get(image_environment)
    if image is None:
        pytest.skip(f"set {image_environment} to a pre-pulled sha256 image digest")

    async def scenario() -> None:
        accepted = asyncio.Event()
        release = asyncio.Event()
        receipts: list[SandboxReceipt] = []

        async def on_accept(receipt: SandboxReceipt) -> None:
            receipts.append(receipt)
            accepted.set()
            await release.wait()

        operation_id = f"real-admission-{uuid4().hex}"
        request = SandboxRequest(
            cwd=PurePosixPath("/"),
            policy=SandboxPolicy(image=image),
            argv=("python", "-c", "print('accepted')"),
            operation_id=operation_id,
        )
        async with asyncio.TaskGroup() as group:
            task = group.create_task(OciSandbox(runtime).execute(request, on_accept=on_accept))
            await asyncio.wait_for(accepted.wait(), timeout=20)
            restarted = OciSandbox(runtime)
            observed = await restarted.query(operation_id)
            assert observed.state == "created" and observed.receipt == receipts[0]
            with pytest.raises(SandboxExecutionError):
                await restarted.execute(request, on_accept=on_accept)
            assert (await restarted.query(operation_id)).receipt == receipts[0]
            stopped = await restarted.stop(receipts[0])
            assert stopped.state == "cancelled" and stopped.evidence == {"container_removed": True}
            assert (await restarted.query(operation_id)).state == "unknown"
            task.cancel()
            with pytest.raises(SandboxCancelledError) as cancellation:
                await task
            assert cancellation.value.confirmed_stop

    asyncio.run(scenario())
