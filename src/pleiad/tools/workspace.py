"""Load trusted Tool factories from a workspace-owned Python package."""

import importlib.abc
import importlib.machinery
import importlib.util
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from types import ModuleType
from typing import cast
from uuid import uuid4

from pleiad.tools.zoo import ToolZoo

WorkspaceToolRegistrar = Callable[[ToolZoo, Path], None]


def register_workspace_tools(zoo: ToolZoo, workspace: Path) -> None:
    """Run ``workspace/tools/register_tools`` when the package exists."""
    entrypoint = workspace / "tools" / "__init__.py"
    if not entrypoint.is_file():
        return
    module = _load_workspace_package(entrypoint)
    registrar = getattr(module, "register_tools", None)
    if not callable(registrar):
        raise ValueError(f"Workspace Tool package must define register_tools: {entrypoint}")
    cast(WorkspaceToolRegistrar, registrar)(zoo, workspace)


def _load_workspace_package(entrypoint: Path) -> ModuleType:
    """Import one workspace Tool package directly from current source files."""
    module_name = f"_pleiad_workspace_tools_{uuid4().hex}"
    finder = _FreshWorkspaceFinder(module_name, entrypoint.parent)
    spec = finder.find_spec(module_name, None)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load Workspace Tool package: {entrypoint}")
    module = importlib.util.module_from_spec(spec)
    sys.meta_path.insert(0, finder)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        _remove_workspace_modules(module_name)
        raise
    finally:
        sys.meta_path.remove(finder)
    return module


class _FreshWorkspaceFinder(importlib.abc.MetaPathFinder):
    """Resolve one generated workspace package from uncached source files."""

    def __init__(self, package_name: str, root: Path) -> None:
        self.package_name = package_name
        self.root = root

    def find_spec(
        self,
        fullname: str,
        path: Sequence[str] | None = None,
        target: ModuleType | None = None,
    ) -> importlib.machinery.ModuleSpec | None:
        """Return a source loader for the generated package or its descendants."""
        del path, target
        if fullname != self.package_name and not fullname.startswith(f"{self.package_name}."):
            return None
        relative_parts = fullname.split(".")[1:]
        module_root = self.root.joinpath(*relative_parts)
        package_entrypoint = module_root / "__init__.py"
        if package_entrypoint.is_file():
            return importlib.machinery.ModuleSpec(
                fullname,
                _FreshSourceLoader(package_entrypoint),
                is_package=True,
            )
        module_entrypoint = module_root.with_suffix(".py")
        if module_entrypoint.is_file():
            return importlib.machinery.ModuleSpec(
                fullname,
                _FreshSourceLoader(module_entrypoint),
                is_package=False,
            )
        return None


class _FreshSourceLoader(importlib.abc.Loader):
    """Compile workspace Python directly, bypassing stale bytecode caches."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def create_module(self, spec: importlib.machinery.ModuleSpec) -> ModuleType | None:
        """Use the default module construction path."""
        del spec
        return None

    def exec_module(self, module: ModuleType) -> None:
        """Compile and execute the current source contents."""
        source = self.path.read_text(encoding="utf-8")
        code = compile(source, str(self.path), "exec")
        exec(code, module.__dict__)  # noqa: S102


def _remove_workspace_modules(package_name: str) -> None:
    """Remove a failed generated package and any modules imported below it."""
    for name in tuple(sys.modules):
        if name == package_name or name.startswith(f"{package_name}."):
            sys.modules.pop(name, None)
