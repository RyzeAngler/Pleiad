from __future__ import annotations

from logging.handlers import RotatingFileHandler
from pathlib import Path


def create_rotating_file_handler(
    path: Path,
    *,
    max_bytes: int,
    backup_count: int,
    encoding: str = "utf-8",
) -> RotatingFileHandler:
    """Create a rotating file handler without attaching it to a logger.

    Args:
        path: Destination log file.
        max_bytes: Maximum size of one file before rollover.
        backup_count: Number of rotated files to retain.
        encoding: Text encoding used by the file handler.

    Returns:
        An unattached standard-library rotating file handler.

    Raises:
        TypeError: If either integer limit is a boolean.
        ValueError: If ``max_bytes`` is not positive or ``backup_count`` is negative.
    """
    if isinstance(max_bytes, bool):
        raise TypeError("max_bytes must be an integer, not a boolean")
    if max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer")
    if isinstance(backup_count, bool):
        raise TypeError("backup_count must be an integer, not a boolean")
    if backup_count < 0:
        raise ValueError("backup_count must be a non-negative integer")

    path.parent.mkdir(parents=True, exist_ok=True)
    return RotatingFileHandler(
        path,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding=encoding,
    )
