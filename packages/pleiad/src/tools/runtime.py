import asyncio
from datetime import UTC, datetime

from jsonschema import Draft202012Validator, SchemaError, ValidationError

from pleiad.contracts import JsonObject
from pleiad.errors.base import SystemFault
from pleiad.schemas import validate_json_schema
from pleiad.tools.base import BaseTool, ToolFailure
from pleiad.tools.contracts import ToolCall, ToolContext, ToolResult
from pleiad.tools.errors import (
    InvalidToolArgumentsError,
    InvalidToolOutputError,
    ToolError,
    ToolExecutionFailedError,
    ToolPermissionDeniedError,
    ToolSystemFault,
    ToolTimeoutError,
    TransientToolError,
)
from pleiad.tools.interruption import InterruptionResponse, ToolExecutionOutcome, ToolInterruption


class ToolRuntime:
    """Execute loaded Tools under one consistent runtime policy."""

    def __init__(self) -> None:
        self.semaphores: dict[str, asyncio.Semaphore] = {}
        self.semaphore_limits: dict[str, int] = {}

    def semaphore(self, tool: BaseTool) -> asyncio.Semaphore:
        """Reuse the concurrency limit for one stable Tool code."""
        code = tool.descriptor.code
        limit = tool.policy.max_concurrency
        existing_limit = self.semaphore_limits.get(code)
        if existing_limit is None:
            self.semaphores[code] = asyncio.Semaphore(limit)
            self.semaphore_limits[code] = limit
        elif existing_limit != limit:
            raise ValueError(f"Tool concurrency policy changed for code: {code}")
        return self.semaphores[code]

    async def resume_interruption(
        self,
        tool: BaseTool,
        interruption: ToolInterruption,
        response: InterruptionResponse,
        context: ToolContext,
        *,
        deadline: datetime,
    ) -> ToolExecutionOutcome | None:
        """Resume a pending Tool under its concurrency slot and Run deadline.

        Args:
            tool: Bound Tool that created the interruption.
            interruption: Validated pending Tool execution.
            response: Host-provided resolution for this interruption.
            context: Current, least-privileged Run context.
            deadline: Absolute deadline of the existing Run.

        Returns:
            The resumed Tool outcome, or None when the Tool or Run deadline expires.
        """
        if interruption.tool_code != tool.descriptor.code or context.call_id != interruption.call_id:
            raise ValueError("Tool interruption identity must match its bound Tool context")
        if response.interruption_id != interruption.interruption_id:
            raise ValueError("Tool response must match its pending interruption")
        missing = set(tool.required_permissions) - set(context.grants.capabilities)
        if missing:
            raise ValueError("Current Tool context no longer grants the pending Tool permissions")
        remaining = (deadline - datetime.now(UTC)).total_seconds()
        if remaining <= 0:
            return None
        timeout = asyncio.timeout(min(remaining, tool.policy.timeout_seconds))
        try:
            async with timeout, self.semaphore(tool):
                return await tool.resume_interruption(context, interruption, response)
        except TimeoutError:
            if not timeout.expired():
                raise
            return None

    async def execute(self, tool: BaseTool, call: ToolCall, context: ToolContext) -> ToolExecutionOutcome:
        """Validate, execute, retry, and normalize one Tool call."""
        if call.tool_code != tool.descriptor.code:
            raise ValueError("Tool call code must match the executable Tool")
        if context.call_id != call.call_id:
            raise ValueError("Tool context call_id must match the Tool call")
        started = datetime.now(UTC)
        try:
            validate_json_schema(tool.input_schema)
        except (SchemaError, ValueError) as error:
            raise ToolSystemFault("The Tool input schema is invalid.") from error
        validation = Draft202012Validator(tool.input_schema)
        input_errors = sorted(validation.iter_errors(call.arguments), key=lambda error: error.path)
        if input_errors:
            return self.failure(
                call.call_id,
                started,
                InvalidToolArgumentsError(schema_failure_reason(input_errors[0])),
            )

        missing = set(tool.required_permissions) - set(context.grants.capabilities)
        if missing:
            return self.failure(
                call.call_id,
                started,
                ToolPermissionDeniedError(
                    f"The Tool invocation lacks required permissions: {', '.join(sorted(missing))}."
                ),
            )

        attempts = tool.policy.max_retries + 1 if tool.policy.idempotent else 1
        async with self.semaphore(tool):
            for attempt in range(attempts):
                try:
                    async with asyncio.timeout(tool.policy.timeout_seconds):
                        output = await tool.execute(context, call.arguments)
                    if isinstance(output, ToolInterruption):
                        if output.call_id != call.call_id:
                            raise ValueError("Tool interruption call_id must match the active call")
                        if output.tool_code != tool.descriptor.code:
                            raise ValueError("Tool interruption tool_code must match the active Tool")
                        if output.tool_name != tool.descriptor.name:
                            raise ValueError("Tool interruption tool_name must match the active Tool")
                        if output.arguments != call.arguments:
                            raise ValueError("Tool interruption arguments must match the active call")
                        return output
                    if isinstance(output, ToolFailure):
                        return self.failure(call.call_id, started, output.error, output.output)
                    output_value: object = output
                    if not isinstance(output_value, dict):
                        return self.failure(
                            call.call_id,
                            started,
                            InvalidToolOutputError(),
                        )
                    return ToolResult(
                        call_id=call.call_id,
                        ok=True,
                        output=output_value,
                        started_at=started,
                        finished_at=datetime.now(UTC),
                    )
                except TimeoutError:
                    if attempt + 1 == attempts:
                        return self.failure(
                            call.call_id,
                            started,
                            ToolTimeoutError(f"The Tool exceeded its {tool.policy.timeout_seconds:g} second timeout."),
                        )
                except ToolError as exc:
                    if not isinstance(exc, TransientToolError) or attempt + 1 == attempts:
                        return self.failure(call.call_id, started, exc)
                except SystemFault:
                    raise
                except ValueError:
                    raise
                except Exception as error:
                    raise ToolSystemFault() from error
        return self.failure(
            call.call_id,
            started,
            ToolExecutionFailedError(),
        )

    @staticmethod
    def failure(
        call_id: str,
        started: datetime,
        error: ToolError,
        output: JsonObject | None = None,
    ) -> ToolResult:
        """Build one failed call result while preserving its original identity."""
        return ToolResult(
            call_id=call_id,
            ok=False,
            output=output,
            error_code=error.code,
            error_message=error.message,
            error_type=error.type,
            started_at=started,
            finished_at=datetime.now(UTC),
        )


def schema_failure_reason(error: ValidationError) -> str:
    """Describe a schema failure without copying an untrusted argument value."""
    path = ".".join(str(part) for part in error.absolute_path)
    location = path if path else "root"
    return f"input schema validation failed at {location}"
