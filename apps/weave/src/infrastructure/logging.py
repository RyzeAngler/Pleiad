import logging
import re
from dataclasses import dataclass
from pathlib import Path

from pleiad.logging.filters import ContextFilter
from pleiad.logging.rotation import create_rotating_file_handler

CREDENTIAL_PATTERN = re.compile(
    r"(?i)\b(api[_-]?key|authorization|password|secret|token)\b\s*[:=]\s*(?:bearer\s+)?[^\s,;]+"
)
BEARER_PATTERN = re.compile(r"(?i)\bbearer\s+[^\s,;]+")
QUERY_CREDENTIAL_PATTERN = re.compile(r"(?i)([?&](?:key|ak)=)[^&#\s\"'<>]*")


def redact_log_credentials(message: str) -> str:
    """Remove credential values while preserving URL query diagnostics."""
    redacted = QUERY_CREDENTIAL_PATTERN.sub(lambda match: f"{match.group(1)}[REDACTED]", message)
    redacted = CREDENTIAL_PATTERN.sub(lambda match: f"{match.group(1)}=[REDACTED]", redacted)
    return BEARER_PATTERN.sub("Bearer [REDACTED]", redacted)


class SafeLogFormatter(logging.Formatter):
    """Redact credential assignments while retaining exception causes and stack frames."""

    def format(self, record: logging.LogRecord) -> str:
        """Sanitize the complete rendered log entry, including exception text."""
        return redact_log_credentials(super().format(record))


MANAGED_HANDLERS: list[tuple[logging.Logger, logging.Handler, int, bool]] = []


@dataclass(frozen=True, slots=True)
class LoggingConfig:
    """Configure product console and rotating file sinks."""

    level: str = "INFO"
    directory: Path | None = None
    console: bool = True
    max_file_bytes: int = 30 * 1024 * 1024
    backup_count: int = 7


def configure_logging(config: LoggingConfig) -> None:
    """Install standard logging handlers with SDK correlation enrichment."""
    level = logging.getLevelNamesMapping().get(config.level.upper())
    if level is None:
        raise ValueError(f"Invalid log level: {config.level}")
    shutdown_logging()
    sinks: list[logging.Handler] = []
    if config.directory is not None:
        sinks.append(
            create_rotating_file_handler(
                config.directory / "weave.log", max_bytes=config.max_file_bytes, backup_count=config.backup_count
            )
        )
    if config.console:
        sinks.append(logging.StreamHandler())
    formatter = SafeLogFormatter(
        "%(asctime)s %(levelname)s %(name)s execution=%(execution_id)s session=%(session_id)s "
        "run=%(run_id)s step=%(step_id)s call=%(call_id)s %(message)s"
    )
    for sink in sinks:
        sink.setFormatter(formatter)
        sink.addFilter(ContextFilter())
        for name in ("pleiad", "pleiad_weave"):
            logger = logging.getLogger(name)
            MANAGED_HANDLERS.append((logger, sink, logger.level, logger.propagate))
            logger.addHandler(sink)
            logger.setLevel(level)
            logger.propagate = False


def shutdown_logging() -> None:
    """Close owned sinks and restore the logger state that preceded startup."""
    for logger, sink, level, propagate in reversed(MANAGED_HANDLERS):
        logger.removeHandler(sink)
        logger.setLevel(level)
        logger.propagate = propagate
        sink.close()
    MANAGED_HANDLERS.clear()
