from pathlib import Path

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register an explicit configuration file for selected real image tests."""
    parser.addoption(
        "--live-config",
        type=Path,
        default=None,
        help="Run selected live image tests with credentials and endpoints from this TOML file.",
    )
