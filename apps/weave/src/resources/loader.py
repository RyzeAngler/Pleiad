"""Read reviewed assets bundled in the Weave application distribution."""

from collections.abc import Iterator
from contextlib import contextmanager
from importlib.resources import as_file, files
from importlib.resources.abc import Traversable
from pathlib import Path


def resource_path(name: str) -> Traversable:
    """Resolve a safe relative path beneath the packaged resource directory."""
    parts = name.split("/")
    if not name or "\\" in name or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("resource name must be a normalized relative path")
    return files("pleiad_weave.resources").joinpath(*parts)


def read_resource_bytes(name: str) -> bytes:
    """Read a packaged resource without depending on a source checkout."""
    resource = resource_path(name)
    if not resource.is_file():
        raise FileNotFoundError(name)
    return resource.read_bytes()


def read_resource_text(name: str, *, encoding: str = "utf-8") -> str:
    """Read a text asset from the Weave resource package."""
    return read_resource_bytes(name).decode(encoding)


@contextmanager
def materialize_resource(name: str) -> Iterator[Path]:
    """Yield a filesystem path for a packaged resource for this context's lifetime."""
    resource = resource_path(name)
    if not resource.is_file():
        raise FileNotFoundError(name)
    with as_file(resource) as path:
        yield path
