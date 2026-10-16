import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ConfigDict, Field

from pleiad.contracts import CapabilityGrants, ContractModel, JsonObject
from pleiad.errors.base import SystemFault
from pleiad.tools.base import FunctionTool
from pleiad.tools.contracts import ToolCall, ToolContext, ToolDescriptor, ToolPolicy, ToolResult
from pleiad.tools.errors import ToolErrorCode, TransientToolError
from pleiad.tools.interruption import InterruptionResponse, ToolInterruption
from pleiad.tools.runtime import ToolRuntime

ToolFunction = Callable[[ToolContext, JsonObject], Awaitable[JsonObject | ToolInterruption]]


class LookupArguments(ContractModel):
    """Parameter contract used by the generic FunctionTool fixture."""

    model_config = ConfigDict(strict=True)

    query: str = Field(min_length=1)


def make_tool(
    function: ToolFunction,
    *,
    policy: ToolPolicy | None = None,
    required_permissions: frozenset[str] = frozenset(),
) -> FunctionTool:
    return FunctionTool(
        ToolDescriptor(name="lookup", code="records.lookup", description="Look up a record"),
        function,
        LookupArguments,
        policy=policy,
        required_permissions=required_permissions,
    )


def make_call(arguments: JsonObject, *, code: str = "records.lookup") -> ToolCall:
    return ToolCall(call_id="call-1", tool_code=code, arguments=arguments)


def test_invalid_tool_arguments_are_rejected_before_side_effect() -> None:
    calls = 0

    async def function(context: ToolContext, arguments: JsonObject) -> JsonObject:
        nonlocal calls
        calls += 1
        return {"value": "unexpected"}

    outcome = asyncio.run(
        ToolRuntime().execute(
            make_tool(function),
            make_call({"query": 42}),
            ToolContext(call_id="call-1"),
        )
    )

    assert isinstance(outcome, ToolResult)
    assert outcome.error_code == ToolErrorCode.INVALID_ARGUMENTS
    assert outcome.error_message == "input schema validation failed at query"
    assert calls == 0


def test_tool_without_required_grant_has_no_side_effect() -> None:
    calls = 0

    async def function(context: ToolContext, arguments: JsonObject) -> JsonObject:
        nonlocal calls
        calls += 1
        return {"value": "unexpected"}

    outcome = asyncio.run(
        ToolRuntime().execute(
            make_tool(function, required_permissions=frozenset({"records.write"})),
            make_call({"query": "alpha"}),
            ToolContext(call_id="call-1", grants=CapabilityGrants()),
        )
    )

    assert isinstance(outcome, ToolResult)
    assert outcome.error_code == ToolErrorCode.PERMISSION_DENIED
    assert outcome.error_message == "The Tool invocation lacks required permissions: records.write."
    assert calls == 0


@pytest.mark.parametrize(
    ("idempotent", "expected_attempts"),
    [(True, 2), (False, 1)],
)
def test_tool_retry_policy_distinguishes_idempotent_side_effects(idempotent: bool, expected_attempts: int) -> None:
    attempts = 0

    async def function(context: ToolContext, arguments: JsonObject) -> JsonObject:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TransientToolError("The Tool dependency is temporarily unavailable.")
        return {"value": "done"}

    outcome = asyncio.run(
        ToolRuntime().execute(
            make_tool(function, policy=ToolPolicy(max_retries=1, idempotent=idempotent)),
            make_call({"query": "alpha"}),
            ToolContext(call_id="call-1"),
        )
    )

    assert attempts == expected_attempts
    assert isinstance(outcome, ToolResult)
    assert outcome.ok is idempotent


def test_tool_call_code_mismatch_fails_before_execution() -> None:
    calls = 0

    async def function(context: ToolContext, arguments: JsonObject) -> JsonObject:
        nonlocal calls
        calls += 1
        return {"value": "unexpected"}

    with pytest.raises(ValueError, match="code must match"):
        asyncio.run(
            ToolRuntime().execute(
                make_tool(function),
                make_call({"query": "alpha"}, code="other.tool"),
                ToolContext(call_id="call-1"),
            )
        )
    assert calls == 0


def test_tool_result_accepts_json_object_output_without_an_output_schema() -> None:
    async def function(context: ToolContext, arguments: JsonObject) -> JsonObject:
        return {"value": 42}

    tool = FunctionTool(
        ToolDescriptor(name="lookup", code="records.lookup", description="Look up a record"),
        function,
        LookupArguments,
    )

    outcome = asyncio.run(
        ToolRuntime().execute(
            tool,
            make_call({"query": "alpha"}),
            ToolContext(call_id="call-1"),
        )
    )

    assert isinstance(outcome, ToolResult)
    assert outcome.ok
    assert outcome.output == {"value": 42}


def test_tool_timeout_error_is_returned_without_waiting_for_a_clock_deadline() -> None:
    async def function(context: ToolContext, arguments: JsonObject) -> JsonObject:
        raise TimeoutError

    outcome = asyncio.run(
        ToolRuntime().execute(
            make_tool(function, policy=ToolPolicy(timeout_seconds=1.5)),
            make_call({"query": "alpha"}),
            ToolContext(call_id="call-1"),
        )
    )

    assert isinstance(outcome, ToolResult)
    assert outcome.error_code == ToolErrorCode.TIMEOUT
    assert outcome.error_message == "The Tool exceeded its 1.5 second timeout."


def test_invalid_tool_interruption_identity_fails_clearly() -> None:
    async def function(context: ToolContext, arguments: JsonObject) -> ToolInterruption:
        return ToolInterruption(
            call_id="other-call",
            tool_code="records.lookup",
            tool_name="lookup",
            arguments=arguments,
            reason="approval required",
        )

    with pytest.raises(ValueError, match="interruption call_id"):
        asyncio.run(
            ToolRuntime().execute(
                make_tool(function),
                make_call({"query": "alpha"}),
                ToolContext(call_id="call-1"),
            )
        )


def test_unclassified_tool_failure_becomes_a_system_fault_with_its_cause() -> None:
    cause = RuntimeError("private implementation detail")

    async def function(context: ToolContext, arguments: JsonObject) -> JsonObject:
        raise cause

    with pytest.raises(SystemFault) as caught:
        asyncio.run(
            ToolRuntime().execute(
                make_tool(function),
                make_call({"query": "alpha"}),
                ToolContext(call_id="call-1"),
            )
        )

    assert caught.value.__cause__ is cause
    assert caught.value.code == ToolErrorCode.RUNTIME_UNAVAILABLE


def test_cancelling_tool_execution_propagates_cancellation() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def function(context: ToolContext, arguments: JsonObject) -> JsonObject:
        started.set()
        await release.wait()
        return {"value": "done"}

    async def scenario() -> None:
        task = asyncio.create_task(
            ToolRuntime().execute(
                make_tool(function),
                make_call({"query": "alpha"}),
                ToolContext(call_id="call-1"),
            )
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())


def test_tool_resume_deadline_includes_waiting_for_a_concurrency_slot() -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        async def function(context: ToolContext, arguments: JsonObject) -> JsonObject:
            started.set()
            await release.wait()
            return {"value": "done"}

        tool = make_tool(function, policy=ToolPolicy(max_concurrency=1))
        runtime = ToolRuntime()
        interruption = ToolInterruption(
            call_id="call-2",
            tool_code="records.lookup",
            tool_name="lookup",
            arguments={"query": "alpha"},
            reason="approval required",
        )
        now = datetime.now(UTC)
        response = InterruptionResponse(
            interruption_id=interruption.interruption_id,
            result=ToolResult(call_id="call-2", ok=True, output={"approved": True}, started_at=now, finished_at=now),
        )
        async with asyncio.TaskGroup() as group:
            active = group.create_task(
                runtime.execute(tool, make_call({"query": "alpha"}), ToolContext(call_id="call-1"))
            )
            await started.wait()
            try:
                outcome = await asyncio.wait_for(
                    runtime.resume_interruption(
                        tool,
                        interruption,
                        response,
                        ToolContext(call_id="call-2"),
                        deadline=datetime.now(UTC) + timedelta(milliseconds=10),
                    ),
                    timeout=1,
                )
                assert outcome is None
            finally:
                release.set()
        assert isinstance(active.result(), ToolResult)

    asyncio.run(scenario())
