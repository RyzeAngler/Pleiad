from pleiad.models.anthropic.client import AnthropicModelClient
from pleiad.models.openai.client import OpenAIModelClient
from pleiad.observability.tracing import Observability
from pleiad.sandbox.backends.oci import OciSandbox
from pleiad.sandbox.sandbox import Sandbox
from pleiad.sessions.backends.sqlite import SqliteSessionStore

from pleiad_weave.agent.weave import WeaveAgents
from pleiad_weave.application.service import ExecutionService
from pleiad_weave.capabilities.catalog import load_capabilities
from pleiad_weave.infrastructure.config import Settings
from pleiad_weave.infrastructure.logging import LoggingConfig, configure_logging
from pleiad_weave.infrastructure.models import create_model
from pleiad_weave.infrastructure.sandbox.provisioning import default_policy
from pleiad_weave.infrastructure.storage.memory import FactStore
from pleiad_weave.infrastructure.storage.observations import DirectoryObserver
from pleiad_weave.infrastructure.storage.product import ProductStore
from pleiad_weave.infrastructure.storage.product_content import ProductContent
from pleiad_weave.infrastructure.storage.product_details import ProductDetails


def create_application(
    settings: Settings | None = None, *, observability: Observability | None = None
) -> ExecutionService:
    """Compose one durable product owner around the public Pleiad runtime.

    Args:
        settings: Validated host configuration, or the deployment environment.
        observability: Optional host-owned telemetry integration.

    Returns:
        The shared service used by HTTP, CLI, and TUI.
    """
    selected = Settings() if settings is None else settings
    telemetry = Observability() if observability is None else observability
    policy = default_policy(selected.sandbox_image)
    selected.workspace.mkdir(parents=True, exist_ok=True)
    store = ProductStore(selected.runtime_root, queue_capacity=selected.queue_capacity)
    sessions = SqliteSessionStore(selected.runtime_root / "sessions.sqlite3")
    content = ProductContent(store)
    details = ProductDetails(store)
    sandbox = Sandbox(OciSandbox(selected.sandbox_runtime, observability=telemetry))
    load_capabilities(selected.workspace, selected.resolved_capability_file, sandbox, policy)
    models: dict[str, OpenAIModelClient | AnthropicModelClient] = {"text": create_model(selected)}
    for role, name in (("vision", selected.llm_vision_model), ("speech", selected.llm_speech_model)):
        if name is not None:
            models[role] = create_model(selected, role)
    agents = WeaveAgents(
        models,
        selected.workspace,
        selected.resolved_capability_file,
        sandbox,
        policy,
        store,
        sessions,
        content,
        details,
        telemetry,
        selected.approval_tool_codes,
        facts=FactStore(
            selected.runtime_root, user_id=selected.memory_user_id, cross_space=selected.memory_cross_space
        ),
        memory_recall_max_results=selected.memory_recall_max_results,
    )
    configure_logging(
        LoggingConfig(
            level=selected.log_level,
            directory=selected.runtime_root / "logs" if selected.log_directory is None else selected.log_directory,
            console=selected.log_console,
            backup_count=selected.log_backup_count,
            max_file_bytes=selected.log_max_file_bytes,
        )
    )
    observer = (
        None
        if selected.observe_directory is None
        else DirectoryObserver(
            store,
            selected.observe_directory,
            max_files=selected.observe_max_files,
            max_file_bytes=selected.observe_max_file_bytes,
            max_total_bytes=selected.observe_max_total_bytes,
        )
    )
    return ExecutionService(
        agents, store, sessions, content, details, max_workers=selected.max_workers, observer=observer
    )
