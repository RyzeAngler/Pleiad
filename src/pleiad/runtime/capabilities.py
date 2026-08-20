"""Hot-reload the workspace Tool and Skill capability catalog."""

import hashlib
from dataclasses import dataclass
from pathlib import Path
from threading import RLock

from loguru import logger

from pleiad.errors import ErrorCode, ErrorInfo, make_error
from pleiad.runtime.events import EventBus
from pleiad.skills.builtins import register_file_skills
from pleiad.skills.manifest import load_skill_descriptors
from pleiad.skills.zoo import SkillZoo
from pleiad.tools.builtins import register_builtins
from pleiad.tools.manifest import load_tool_descriptors
from pleiad.tools.workspace import register_workspace_tools
from pleiad.tools.zoo import ToolZoo


@dataclass(frozen=True, slots=True)
class CapabilitySyncResult:
    """The result of one workspace capability synchronization attempt."""

    changed: bool
    applied: bool
    fingerprint: str
    added_tools: tuple[str, ...] = ()
    removed_tools: tuple[str, ...] = ()
    added_skills: tuple[str, ...] = ()
    removed_skills: tuple[str, ...] = ()
    error: ErrorInfo | None = None


class WorkspaceCapabilitySync:
    """Keep process-scoped Zoos synchronized with the workspace files."""

    def __init__(
        self,
        tools: ToolZoo,
        skills: SkillZoo,
        workspace: Path,
        capability_file: Path,
        events: EventBus | None = None,
    ) -> None:
        self.tools = tools
        self.skills = skills
        self.workspace = workspace.resolve()
        self.capability_file = capability_file.resolve()
        self.events = events
        self._lock = RLock()
        self._last_attempted_fingerprint: str | None = None
        self._last_applied_fingerprint: str | None = None
        self._last_error: ErrorInfo | None = None

    def prime(self) -> None:
        """Mark the already bootstrapped catalog as the current workspace version."""
        with self._lock:
            self._last_attempted_fingerprint = _workspace_fingerprint(
                self.workspace,
                self.capability_file,
            )
            self._last_applied_fingerprint = self._last_attempted_fingerprint
            self._last_error = None

    def sync(self, *, execution_id: str | None = None) -> CapabilitySyncResult:
        """Apply a changed workspace catalog while retaining the last valid version on error."""
        try:
            fingerprint = _workspace_fingerprint(self.workspace, self.capability_file)
        except Exception as exc:
            info = _reload_error(exc)
            self._emit_failure(execution_id, info)
            return CapabilitySyncResult(
                changed=True,
                applied=False,
                fingerprint="",
                error=info,
            )

        with self._lock:
            if fingerprint == self._last_attempted_fingerprint:
                return CapabilitySyncResult(
                    changed=False,
                    applied=False,
                    fingerprint=fingerprint,
                    error=self._last_error,
                )

            previous_tools = set(self.tools.codes())
            previous_skills = {descriptor.code for descriptor in self.skills.snapshot().descriptors}
            try:
                candidate_tools, candidate_skills = self._build_candidate()
            except Exception as exc:
                info = _reload_error(exc)
                self._last_attempted_fingerprint = fingerprint
                self._last_error = info
                self._emit_failure(execution_id, info)
                return CapabilitySyncResult(
                    changed=True,
                    applied=False,
                    fingerprint=fingerprint,
                    error=info,
                )

            current_tools = set(candidate_tools.codes())
            current_skills = {
                descriptor.code for descriptor in candidate_skills.snapshot().descriptors
            }
            self.tools.replace_from(candidate_tools)
            self.skills.replace_from(candidate_skills)
            self._last_attempted_fingerprint = fingerprint
            self._last_applied_fingerprint = fingerprint
            self._last_error = None
            result = CapabilitySyncResult(
                changed=True,
                applied=True,
                fingerprint=fingerprint,
                added_tools=tuple(sorted(current_tools - previous_tools)),
                removed_tools=tuple(sorted(previous_tools - current_tools)),
                added_skills=tuple(sorted(current_skills - previous_skills)),
                removed_skills=tuple(sorted(previous_skills - current_skills)),
            )
            self._emit_success(execution_id, result)
            return result

    def _build_candidate(self) -> tuple[ToolZoo, SkillZoo]:
        """Build a complete candidate catalog without mutating the active one."""
        candidate_tools = ToolZoo()
        register_builtins(candidate_tools, self.workspace)
        register_workspace_tools(candidate_tools, self.workspace)
        for descriptor in load_tool_descriptors(self.capability_file):
            candidate_tools.register_descriptor(descriptor)

        candidate_skills = SkillZoo()
        descriptors = load_skill_descriptors(self.capability_file)
        register_file_skills(candidate_skills, descriptors, self.workspace / "skills")
        return candidate_tools, candidate_skills

    def _emit_success(self, execution_id: str | None, result: CapabilitySyncResult) -> None:
        """Publish a successful reload to the current execution when available."""
        logger.info(
            "能力目录已热加载: tools_added={} skills_added={} tools_removed={} skills_removed={}",
            result.added_tools,
            result.added_skills,
            result.removed_tools,
            result.removed_skills,
        )
        if self.events and execution_id:
            self.events.emit(
                execution_id,
                "capabilities.reloaded",
                {
                    "fingerprint": result.fingerprint,
                    "tools_added": list(result.added_tools),
                    "tools_removed": list(result.removed_tools),
                    "skills_added": list(result.added_skills),
                    "skills_removed": list(result.removed_skills),
                },
            )

    def _emit_failure(self, execution_id: str | None, info: ErrorInfo) -> None:
        """Publish a recoverable reload failure without stopping the Agent loop."""
        logger.error("能力目录热加载失败: {}", info.details)
        if self.events and execution_id:
            self.events.emit(execution_id, "capabilities.reload_failed", info.as_dict())


def _workspace_fingerprint(workspace: Path, capability_file: Path) -> str:
    """Hash only workspace capability sources and the public capability manifest."""
    paths = {capability_file}
    for root, pattern in (
        (workspace / "tools", "*.py"),
        (workspace / "skills", "*.md"),
    ):
        if root.is_dir():
            paths.update(root.rglob(pattern))
    digest = hashlib.sha256()
    for path in sorted(paths):
        digest.update(str(path).encode("utf-8"))
        if path.is_file():
            digest.update(path.read_bytes())
        else:
            digest.update(b"<missing>")
    return digest.hexdigest()


def _reload_error(exc: Exception) -> ErrorInfo:
    """Convert one candidate catalog failure into a recoverable catalog error."""
    return make_error(
        ErrorCode.SYSTEM_CAPABILITY_RELOAD,
        details={"exception": exc.__class__.__name__, "reason": str(exc)},
    )
