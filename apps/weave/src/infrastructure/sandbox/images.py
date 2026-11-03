"""Configured sandbox image references."""

from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path

from pleiad_weave.resources.loader import materialize_resource


@contextmanager
def bundled_python_image_definition() -> Iterator[tuple[Path, Path]]:
    """Yield the bundled Python image Dockerfile and lockfile paths.

    The paths are valid only inside this context.  This keeps provisioning
    independent of the repository checkout and works for zipped installations.
    """
    with ExitStack() as stack:
        dockerfile = stack.enter_context(materialize_resource("sandbox/images/python/Dockerfile"))
        requirements_lock = stack.enter_context(materialize_resource("sandbox/images/python/requirements.lock"))
        yield dockerfile, requirements_lock
