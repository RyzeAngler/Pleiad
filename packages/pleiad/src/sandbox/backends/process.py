import asyncio
from asyncio.subprocess import Process
from dataclasses import dataclass

PROCESS_READ_SIZE = 64 * 1024
PROCESS_TERMINATION_TIMEOUT_SECONDS = 2.0


@dataclass(frozen=True, slots=True)
class ProcessResult:
    """Bounded facts collected from one OCI CLI process."""

    returncode: int | None
    stdout: bytes
    stderr: bytes
    timed_out: bool
    duration_seconds: float
    output_truncated: bool


async def run_process(
    command: tuple[str, ...],
    *,
    stdin: bytes | None,
    output_limit: int,
    timeout_seconds: float | None,
) -> ProcessResult:
    """Run one executable without a shell while collecting both streams with a bound."""
    if not command:
        raise ValueError("OCI process command must not be empty")
    if output_limit <= 0:
        raise ValueError("OCI process output limit must be positive")
    process = await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    started = asyncio.get_running_loop().time()
    return await collect_process(process, stdin, output_limit, timeout_seconds, started)


async def collect_process(
    process: Process,
    stdin: bytes | None,
    output_limit: int,
    timeout_seconds: float | None,
    started: float,
) -> ProcessResult:
    """Collect process streams and lifecycle facts concurrently."""
    stdout_stream = process.stdout
    stderr_stream = process.stderr
    if stdout_stream is None or stderr_stream is None:
        raise OSError("OCI process did not expose stdout and stderr pipes")
    timed_out = False
    cancelled = False
    async with asyncio.TaskGroup() as group:
        stdout_task = group.create_task(collect_output(stdout_stream, output_limit))
        stderr_task = group.create_task(collect_output(stderr_stream, output_limit))
        wait_task = group.create_task(process.wait())
        input_task = group.create_task(send_stdin(process, stdin)) if stdin is not None else None
        try:
            if timeout_seconds is None:
                await asyncio.shield(wait_task)
            else:
                await asyncio.wait_for(asyncio.shield(wait_task), timeout=timeout_seconds)
        except TimeoutError:
            timed_out = True
            await terminate_process(process)
            await wait_task
        except asyncio.CancelledError:
            cancelled = True
            await terminate_process(process)
            await wait_task
        if input_task is not None:
            await asyncio.shield(input_task)
    if cancelled:
        raise asyncio.CancelledError
    stdout, stdout_truncated = stdout_task.result()
    stderr, stderr_truncated = stderr_task.result()
    return ProcessResult(
        returncode=process.returncode,
        stdout=stdout,
        stderr=stderr,
        timed_out=timed_out,
        duration_seconds=asyncio.get_running_loop().time() - started,
        output_truncated=stdout_truncated or stderr_truncated,
    )


async def collect_output(stream: asyncio.StreamReader, limit: int) -> tuple[bytes, bool]:
    """Read a stream to EOF while retaining at most its configured prefix."""
    chunks: list[bytes] = []
    retained = 0
    truncated = False
    while True:
        chunk = await stream.read(PROCESS_READ_SIZE)
        if not chunk:
            break
        remaining = limit - retained
        if remaining > 0:
            chunks.append(chunk[:remaining])
            retained += min(len(chunk), remaining)
        if len(chunk) > remaining:
            truncated = True
    return b"".join(chunks), truncated


async def send_stdin(process: Process, data: bytes) -> None:
    """Write request stdin and close the pipe so the command observes EOF."""
    stream = process.stdin
    if stream is None:
        raise OSError("OCI process did not expose an stdin pipe")
    try:
        stream.write(data)
        await stream.drain()
    except BrokenPipeError:
        return
    finally:
        stream.close()
        await stream.wait_closed()


async def terminate_process(process: Process) -> None:
    """Terminate and, if needed, kill one CLI process before returning."""
    if process.returncode is not None:
        return
    try:
        process.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=PROCESS_TERMINATION_TIMEOUT_SECONDS)
    except TimeoutError:
        process.kill()
        await process.wait()
