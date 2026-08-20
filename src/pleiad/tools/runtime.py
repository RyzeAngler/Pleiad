"""Validation, timeout, retry, and safe failure handling for Tool calls."""

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import cast

from jsonschema import Draft202012Validator

from pleiad.errors import ErrorCode, ErrorInfo, PleiadError, make_error
from pleiad.observability import ObservabilityPort, SpanKind, SpanScope, SpanStatus, observed
from pleiad.tools.base import BaseTool
from pleiad.tools.contracts import ToolCall, ToolContext, ToolError, ToolResult


def _span_status(result: ToolResult) -> SpanStatus:
    """Map a normalized Tool result to its lifecycle status."""
    if result.ok:
        return SpanStatus.SUCCEEDED
    if result.error and result.error.code == ErrorCode.TOOL_TIMEOUT:
        return SpanStatus.TIMEOUT
    return SpanStatus.FAILED


def _tool_span_result(result: ToolResult) -> tuple[SpanStatus, ErrorInfo | None]:
    """Project a Tool result into the generic instrumentation contract."""
    return (
        _span_status(result),
        _tool_error_info(result.error) if result.error else None,
    )


def _tool_error_info(error: ToolError) -> ErrorInfo:
    """Project the Tool contract error into the observability error contract."""
    return ErrorInfo(
        code=error.code,
        message=error.message,
        kind=error.kind,
        retryable=error.retryable,
        terminal=error.terminal,
        details=error.details,
    )


def _link_tool_span(
    observability: ObservabilityPort,
    span: SpanScope,
    args: tuple[object, ...],
    kwargs: Mapping[str, object],
) -> None:
    """Keep Model-to-Tool causality as an explicit Trace Link."""
    call = cast(ToolCall, args[2])
    source_span_id = kwargs.get("caused_by_span_id")
    if isinstance(source_span_id, str):
        observability.link(
            span.context.trace_id,
            target_span_id=span.span_id,
            source_span_id=source_span_id,
            relation="caused_by",
            correlation_id=call.call_id,
        )


class ToolRuntime:
    """Execute loaded Tools under one consistent runtime policy."""

    def __init__(self, observability: ObservabilityPort | None = None) -> None:
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._semaphore_limits: dict[str, int] = {}
        self.observability = observability

    def _semaphore(self, tool: BaseTool) -> asyncio.Semaphore:
        code = tool.descriptor.code
        limit = tool.policy.max_concurrency
        if self._semaphore_limits.get(code) != limit:
            self._semaphores[code] = asyncio.Semaphore(limit)
            self._semaphore_limits[code] = limit
        return self._semaphores[code]

    @observed(
        name="tool.execute",
        kind=SpanKind.TOOL,
        attributes=lambda args, kwargs: {
            "tool_code": getattr(args[2], "tool_code", "unknown"),
            "call_id": getattr(args[2], "call_id", "unknown"),
        },
        result_projector=lambda result: _tool_span_result(cast(ToolResult, result)),
        on_start=_link_tool_span,
    )
    async def execute(
        self,
        tool: BaseTool,
        call: ToolCall,
        context: ToolContext,
        *,
        caused_by_span_id: str | None = None,
    ) -> ToolResult:
        """Execute one Tool with an automatically managed Tool Span."""
        return await self._execute(tool, call, context)

    async def _execute(self, tool: BaseTool, call: ToolCall, context: ToolContext) -> ToolResult:
        """Validate, execute, retry, and normalize one Tool call."""
        started = datetime.now(UTC)
        validation = Draft202012Validator(tool.input_schema)
        input_errors = sorted(validation.iter_errors(call.arguments), key=lambda error: error.path)
        if input_errors:
            return self._failure(
                call.call_id,
                started,
                make_error(
                    ErrorCode.TOOL_INVALID_ARGUMENTS,
                    details={"reason": input_errors[0].message},
                ),
            )

        required_permissions: frozenset[str] = frozenset(
            getattr(tool, "required_permissions", frozenset())
        )
        missing = set(required_permissions) - set(context.permissions)
        if missing:
            return self._failure(
                call.call_id,
                started,
                make_error(
                    ErrorCode.TOOL_PERMISSION,
                    details={"missing_permissions": sorted(missing)},
                ),
            )

        attempts = tool.policy.max_retries + 1 if tool.policy.idempotent else 1
        async with self._semaphore(tool):
            for attempt in range(attempts):
                try:
                    async with asyncio.timeout(tool.policy.timeout_seconds):
                        output = await tool.execute(context, call.arguments)
                    output_errors = sorted(
                        Draft202012Validator(tool.output_schema).iter_errors(output),
                        key=lambda error: error.path,
                    )
                    if output_errors:
                        return self._failure(
                            call.call_id,
                            started,
                            make_error(
                                ErrorCode.TOOL_OUTPUT,
                                details={"reason": output_errors[0].message},
                            ),
                        )
                    return ToolResult(
                        call_id=call.call_id,
                        ok=True,
                        output=output,
                        started_at=started,
                        finished_at=datetime.now(UTC),
                    )
                except TimeoutError:
                    if attempt + 1 == attempts:
                        return self._failure(
                            call.call_id,
                            started,
                            make_error(
                                ErrorCode.TOOL_TIMEOUT,
                                details={"timeout_seconds": tool.policy.timeout_seconds},
                                retryable=tool.policy.idempotent,
                            ),
                        )
                except PleiadError as exc:
                    if not exc.info.retryable or attempt + 1 == attempts:
                        return self._failure(call.call_id, started, exc.info)
                except Exception as exc:
                    info = make_error(
                        ErrorCode.TOOL_EXECUTION,
                        details={"exception": exc.__class__.__name__},
                        retryable=tool.policy.idempotent,
                    )
                    if attempt + 1 == attempts:
                        return self._failure(
                            call.call_id,
                            started,
                            info,
                        )
        return self._failure(
            call.call_id,
            started,
            make_error(ErrorCode.TOOL_EXECUTION),
        )

    @staticmethod
    def _failure(
        call_id: str,
        started: datetime,
        info: ErrorInfo,
    ) -> ToolResult:
        return ToolResult(
            call_id=call_id,
            ok=False,
            error=ToolError(
                code=info.code,
                message=info.message,
                kind=info.kind,
                retryable=info.retryable,
                terminal=info.terminal,
                details=info.details,
            ),
            started_at=started,
            finished_at=datetime.now(UTC),
        )
