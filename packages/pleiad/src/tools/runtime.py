import asyncio
from datetime import UTC, datetime

from jsonschema import Draft202012Validator, SchemaError, ValidationError

from pleiad.contracts import JsonObject
from pleiad.errors.base import BusinessError, SystemFault
from pleiad.schemas import validate_json_schema
from pleiad.tools.base import BackgroundAdmission, BaseTool, ToolFailure
from pleiad.tools.contracts import ToolCall, ToolContext, ToolResult
from pleiad.tools.errors import (
    InvalidToolArgumentsError,
    InvalidToolOutputError,
    ToolCancelledError,
    ToolExecutionFailedError,
    ToolPermissionDeniedError,
    ToolSystemFault,
    ToolTimeoutError,
    TransientToolError,
)
from pleiad.tools.interruption import InterruptionResponse, ToolAuthorization, ToolExecutionOutcome, ToolInterruption


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
        if interruption.phase == "authorization":
            if not response.result.ok:
                return response.result
            if tool.policy.background:
                raise ValueError("Background authorization must resume through its owning Run operation scope")
            authorization_timeout = asyncio.timeout(remaining)
            try:
                async with authorization_timeout:
                    return await self.execute(
                        tool,
                        ToolCall(
                            call_id=interruption.call_id,
                            tool_code=interruption.tool_code,
                            arguments=interruption.arguments,
                        ),
                        context,
                        authorization=ToolAuthorization(interruption, response),
                    )
            except TimeoutError:
                if not authorization_timeout.expired():
                    raise
                return None
        timeout = asyncio.timeout(min(remaining, tool.policy.timeout_seconds))
        try:
            async with timeout, self.semaphore(tool):
                outcome = await tool.resume_interruption(context, interruption, response)
                return attach_usage(tool, context, outcome)
        except TimeoutError:
            if not timeout.expired():
                raise
            return None

    async def prepare(
        self, tool: BaseTool, call: ToolCall, context: ToolContext, *, authorization: ToolAuthorization | None = None
    ) -> ToolExecutionOutcome | None:
        """Validate current authority and obtain authorization before backend admission."""
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

        if authorization is not None:
            pending = authorization.interruption
            if (
                pending.tool_code != tool.descriptor.code
                or pending.tool_name != tool.descriptor.name
                or pending.call_id != call.call_id
                or pending.arguments != call.arguments
            ):
                raise ValueError("Tool authorization does not match the admitted action")
            return None
        requested = await tool.authorize(context, call.arguments)
        if requested is None:
            return None
        if (
            requested.tool_code != tool.descriptor.code
            or requested.tool_name != tool.descriptor.name
            or requested.call_id != call.call_id
            or requested.arguments != call.arguments
        ):
            raise ValueError("Tool authorization request must retain the original action")
        return attach_usage(tool, context, requested.model_copy(update={"phase": "authorization"}))

    async def execute(
        self,
        tool: BaseTool,
        call: ToolCall,
        context: ToolContext,
        *,
        admission: BackgroundAdmission | None = None,
        authorization: ToolAuthorization | None = None,
    ) -> ToolExecutionOutcome:
        """Validate, authorize, execute, retry, and normalize one Tool call."""
        prepared = await self.prepare(tool, call, context, authorization=authorization)
        if prepared is not None:
            return prepared
        started = datetime.now(UTC)
        attempts = tool.policy.max_retries + 1 if tool.policy.idempotent else 1
        async with self.semaphore(tool):
            for attempt in range(attempts):
                try:
                    async with asyncio.timeout(tool.policy.timeout_seconds):
                        if admission is None:
                            output = await tool.execute(context, call.arguments)
                        else:
                            if not tool.supports_background:
                                raise ToolSystemFault("The Tool does not implement background backend acceptance.")
                            admission.entered = True
                            output = await tool.execute_background(context, call.arguments, admission)
                    if isinstance(output, ToolInterruption):
                        if admission is not None:
                            raise ValueError("Background authorization must resolve before backend admission")
                        if output.call_id != call.call_id:
                            raise ValueError("Tool interruption call_id must match the active call")
                        if output.tool_code != tool.descriptor.code:
                            raise ValueError("Tool interruption tool_code must match the active Tool")
                        if output.tool_name != tool.descriptor.name:
                            raise ValueError("Tool interruption tool_name must match the active Tool")
                        if output.arguments != call.arguments:
                            raise ValueError("Tool interruption arguments must match the active call")
                        return attach_usage(tool, context, output)
                    if isinstance(output, ToolFailure):
                        return self.failure(call.call_id, started, output.error, output.output)
                    output_value: object = output
                    if not isinstance(output_value, dict):
                        return self.failure(
                            call.call_id,
                            started,
                            InvalidToolOutputError(),
                        )
                    return attach_usage(
                        tool,
                        context,
                        ToolResult(
                            call_id=call.call_id,
                            ok=True,
                            output=output_value,
                            started_at=started,
                            finished_at=datetime.now(UTC),
                        ),
                    )
                except TimeoutError as error:
                    if admission is not None and admission.entered:
                        cause = error.__cause__
                        if not isinstance(cause, ToolCancelledError) or not cause.confirmed_stop:
                            raise ToolSystemFault(
                                "The background Tool timed out without confirmed backend settlement."
                            ) from error
                    if attempt + 1 == attempts or (admission is not None and admission.receipt is not None):
                        return self.failure(
                            call.call_id,
                            started,
                            ToolTimeoutError(f"The Tool exceeded its {tool.policy.timeout_seconds:g} second timeout."),
                        )
                except BusinessError as exc:
                    if (
                        not isinstance(exc, TransientToolError)
                        or attempt + 1 == attempts
                        or (admission is not None and admission.receipt is not None)
                    ):
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
        error: BusinessError,
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


def attach_usage(tool: BaseTool, context: ToolContext, outcome: ToolExecutionOutcome) -> ToolExecutionOutcome:
    """Attach trusted host ledger facts independently of model-visible output JSON."""
    ledger = tool.budget_ledger
    if ledger is None or context.run_id is None:
        return outcome
    return outcome.model_copy(update={"usage_charges": ledger.usage_charges(context.run_id)})
