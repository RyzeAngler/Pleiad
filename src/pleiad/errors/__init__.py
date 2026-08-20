"""Unified Pleiad error library for Agent, Tool, and system boundaries."""

from pleiad.errors.base import (
    AgentError,
    ErrorCode,
    ErrorInfo,
    ErrorKind,
    PleiadError,
    SystemError,
    ToolExecutionError,
)
from pleiad.errors.catalog import make_error

__all__ = [
    "AgentError",
    "ErrorCode",
    "ErrorInfo",
    "ErrorKind",
    "PleiadError",
    "SystemError",
    "ToolExecutionError",
    "make_error",
]
