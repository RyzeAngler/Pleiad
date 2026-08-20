"""Low-intrusion instrumentation for asynchronous execution boundaries."""

from __future__ import annotations

from collections.abc import Callable, Coroutine, Mapping
from functools import wraps
from typing import Any, ParamSpec, TypeVar, cast

from loguru import logger

from pleiad.errors import ErrorInfo
from pleiad.observability.models import SpanKind, SpanStatus
from pleiad.observability.port import ObservabilityPort, observe_span
from pleiad.observability.tracer import SpanScope

P = ParamSpec("P")
R = TypeVar("R")

AttributeFactory = Callable[[tuple[object, ...], Mapping[str, object]], Mapping[str, Any]]
ResultProjector = Callable[[object], tuple[SpanStatus, ErrorInfo | None]]
SpanStartHook = Callable[
    [ObservabilityPort, SpanScope, tuple[object, ...], Mapping[str, object]], None
]


def observed(
    *,
    name: str,
    kind: SpanKind,
    attributes: Mapping[str, Any] | AttributeFactory | None = None,
    result_projector: ResultProjector | None = None,
    on_start: SpanStartHook | None = None,
    observability_attribute: str = "observability",
) -> Callable[
    [Callable[P, Coroutine[Any, Any, R]]],
    Callable[P, Coroutine[Any, Any, R]],
]:
    """Decorate an async boundary with automatic Span lifecycle handling.

    The decorated method only needs to expose an ``observability`` field. Span
    creation, parent propagation, terminal status, and error projection stay
    outside the business method. Observability failures are deliberately
    isolated from the execution being observed.
    """

    def decorate(
        function: Callable[P, Coroutine[Any, Any, R]],
    ) -> Callable[P, Coroutine[Any, Any, R]]:
        @wraps(function)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            owner = args[0] if args else None
            observability = cast(
                ObservabilityPort | None,
                getattr(owner, observability_attribute, None),
            )
            resolved_attributes = _resolve_attributes(attributes, args, kwargs)
            with observe_span(
                observability,
                name,
                kind=kind,
                attributes=resolved_attributes,
            ) as span:
                if span is not None and on_start is not None and observability is not None:
                    _safe_on_start(on_start, observability, span, args, kwargs)
                result = await function(*args, **kwargs)
                if span is not None:
                    if result_projector is not None:
                        status, error = _safe_project_result(result_projector, result)
                    else:
                        status, error = _default_result_projector(result)
                    span.set_status(status, error)
                return result

        return wrapper

    return decorate


def _resolve_attributes(
    source: Mapping[str, Any] | AttributeFactory | None,
    args: tuple[object, ...],
    kwargs: Mapping[str, object],
) -> dict[str, Any]:
    if source is None:
        return {}
    if callable(source):
        try:
            return dict(source(args, kwargs))
        except Exception:
            logger.exception("Trace 属性生成失败")
            return {}
    return dict(source)


def _default_result_projector(result: object) -> tuple[SpanStatus, ErrorInfo | None]:
    """Project common Runtime result contracts without importing Runtime types."""
    raw_status = getattr(result, "status", None)
    if raw_status is not None:
        value = str(getattr(raw_status, "value", raw_status))
        status = {
            "succeeded": SpanStatus.SUCCEEDED,
            "cancelled": SpanStatus.CANCELLED,
            "timeout": SpanStatus.TIMEOUT,
        }.get(value, SpanStatus.FAILED)
    else:
        status = SpanStatus.SUCCEEDED if bool(getattr(result, "ok", False)) else SpanStatus.FAILED
    error = getattr(result, "error", None)
    return status, error if isinstance(error, ErrorInfo) else None


def _safe_on_start(
    hook: SpanStartHook,
    observability: ObservabilityPort,
    span: SpanScope,
    args: tuple[object, ...],
    kwargs: Mapping[str, object],
) -> None:
    try:
        hook(observability, span, args, kwargs)
    except Exception:
        logger.exception("Trace Span 初始化钩子执行失败: {}", span.name)


def _safe_project_result(
    projector: ResultProjector,
    result: object,
) -> tuple[SpanStatus, ErrorInfo | None]:
    try:
        return projector(result)
    except Exception:
        logger.exception("Trace 结果状态投影失败")
        return SpanStatus.SUCCEEDED, None
