"""Public entrypoint for the Pleiad terminal workbench."""

from pleiad.application.service import ExecutionService
from pleiad.tui.app import PleiadApp


def run_tui(service: ExecutionService) -> None:
    """Run the Textual application for one process-scoped service."""
    PleiadApp(service).run()


__all__ = ["run_tui"]
