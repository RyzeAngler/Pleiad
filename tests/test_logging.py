from __future__ import annotations

import logging

from pleiad.logging import _is_project_logger


def test_only_pleiad_standard_loggers_are_intercepted() -> None:
    assert _is_project_logger("pleiad")
    assert _is_project_logger("pleiad.runtime.events")
    assert not _is_project_logger("markdown_it.rules_block.paragraph")
    assert not _is_project_logger("httpcore.connection")


def test_project_logger_names_are_not_confused_with_similar_names() -> None:
    record = logging.LogRecord(
        name="pleiads.third_party",
        level=logging.DEBUG,
        pathname=__file__,
        lineno=1,
        msg="not a Pleiad logger",
        args=(),
        exc_info=None,
    )

    assert not _is_project_logger(record.name)
