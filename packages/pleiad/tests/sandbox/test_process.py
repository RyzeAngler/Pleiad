import asyncio

import pytest

from pleiad.sandbox.backends.process import collect_output, run_process


async def stream_with(data: bytes) -> asyncio.StreamReader:
    """Create an in-memory EOF-terminated stream for the collector boundary."""
    stream = asyncio.StreamReader()
    stream.feed_data(data)
    stream.feed_eof()
    return stream


class FakeWriter:
    """Capture stdin writes and EOF closure at the subprocess boundary."""

    def __init__(self) -> None:
        self.data = bytearray()
        self.closed = False

    def write(self, data: bytes) -> None:
        self.data.extend(data)

    async def drain(self) -> None:
        """Complete a deterministic drain operation."""

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        """Complete a deterministic close operation."""


class FakeProcess:
    """Minimal subprocess boundary with two preloaded streams."""

    def __init__(self, stdin_enabled: bool) -> None:
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.stdin = FakeWriter() if stdin_enabled else None
        self.returncode: int | None = 0
        self.terminated = False
        self.stdout.feed_data(b"o" * 100_000)
        self.stdout.feed_eof()
        self.stderr.feed_data(b"e" * 100_000)
        self.stderr.feed_eof()

    async def wait(self) -> int:
        """Return the deterministic process exit code."""
        if self.returncode is None:
            raise AssertionError("fake process has no exit code")
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.terminated = True
        self.returncode = -9


def test_collect_output_drains_large_stream_while_retaining_only_the_prefix() -> None:
    async def scenario() -> None:
        stdout = await collect_output(await stream_with(b"o" * 100_000), 257)
        stderr = await collect_output(await stream_with(b"e" * 100_000), 193)

        assert stdout == (b"o" * 257, True)
        assert stderr == (b"e" * 193, True)

    asyncio.run(scenario())


def test_run_process_drains_both_streams_and_closes_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        processes: list[FakeProcess] = []

        async def fake_create(
            *command: str,
            stdin: int,
            stdout: int,
            stderr: int,
        ) -> FakeProcess:
            del command, stdout, stderr
            process = FakeProcess(stdin_enabled=stdin == asyncio.subprocess.PIPE)
            processes.append(process)
            return process

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
        result = await run_process(
            ("docker", "start", "--attach", "container-id"),
            stdin=b"input",
            output_limit=257,
            timeout_seconds=None,
        )

        assert result.returncode == 0
        assert result.stdout == b"o" * 257
        assert result.stderr == b"e" * 257
        assert result.output_truncated is True
        assert processes[0].stdin is not None
        assert bytes(processes[0].stdin.data) == b"input"
        assert processes[0].stdin.closed is True

    asyncio.run(scenario())
