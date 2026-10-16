from __future__ import annotations

import logging

from pleiad.logging.context import LOG_CONTEXT_FIELDS, current_log_context


class ContextFilter(logging.Filter):
    """Attach the current correlation fields to each log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Set each correlation field from the current context.

        Args:
            record: The record to enrich.

        Returns:
            Always ``True`` because the filter does not discard records.
        """
        context = current_log_context()
        for field in LOG_CONTEXT_FIELDS:
            setattr(record, field, getattr(context, field))
        return True
