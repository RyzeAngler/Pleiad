from pleiad.models.anthropic.client import AnthropicModelClient
from pleiad.models.openai.client import OpenAIModelClient
from pleiad.observability.tracing import Observability
from pleiad.orchestration.graph.executor import GraphExecutor
from pleiad.sandbox.backends.oci import OciSandbox
from pleiad.sandbox.sandbox import Sandbox

from pleiad_weave.agent.graph import GraphRunner
from pleiad_weave.agent.weave import WeaveAgent
from pleiad_weave.application.events import ExecutionEventBus, ExecutionEventPublisher
from pleiad_weave.application.service import ExecutionService
from pleiad_weave.capabilities.catalog import load_capabilities
from pleiad_weave.infrastructure.config import Settings
from pleiad_weave.infrastructure.logging import LoggingConfig, configure_logging
from pleiad_weave.infrastructure.models import create_model
from pleiad_weave.infrastructure.sandbox.provisioning import default_policy
from pleiad_weave.infrastructure.storage.artifacts import ArtifactStore
from pleiad_weave.infrastructure.storage.graphs import TaskGraphStore
from pleiad_weave.infrastructure.storage.memory import MemoryStore
from pleiad_weave.infrastructure.storage.repository import ExecutionRepository, GraphRunRepository, NodeRunRepository


def create_application(
    settings: Settings | None = None, *, observability: Observability | None = None
) -> ExecutionService:
    """Compose Weave through current public SDK APIs.

    Args:
        settings: Explicit deployment settings, or environment-derived settings.
        observability: Host-owned SDK telemetry. Its exporter lifecycle remains
            with the caller that created it.

    Returns:
        The shared CLI, HTTP, and TUI product service.
    """
    selected = Settings() if settings is None else settings
    telemetry = Observability() if observability is None else observability
    policy = default_policy(selected.sandbox_image)
    selected.workspace.mkdir(parents=True, exist_ok=True)
    sandbox = Sandbox(OciSandbox(selected.sandbox_runtime, observability=telemetry))
    load_capabilities(selected.workspace, selected.resolved_capability_file, sandbox, policy)
    repository = ExecutionRepository(selected.runtime_root / "executions")
    events = ExecutionEventBus()
    publisher = ExecutionEventPublisher(repository, events)
    models: dict[str, OpenAIModelClient | AnthropicModelClient] = {"text": create_model(selected)}
    for role, model in (("vision", selected.llm_vision_model), ("speech", selected.llm_speech_model)):
        if model is not None:
            models[role] = create_model(selected, role)
    graphs = TaskGraphStore(selected.runtime_root / "graphs")
    graph_executor = GraphRunner(
        GraphExecutor(observability=telemetry),
        publisher,
        GraphRunRepository(selected.runtime_root / "graph-runs"),
        NodeRunRepository(selected.runtime_root / "node-runs"),
        ArtifactStore(selected.runtime_root / "artifacts"),
    )
    memory = MemoryStore(
        selected.runtime_root / "memory",
        user_id=selected.memory_user_id,
        cross_conversation=selected.memory_cross_conversation,
    )
    weave = WeaveAgent(
        models,
        selected.workspace,
        selected.resolved_capability_file,
        sandbox,
        policy,
        memory,
        graphs,
        graph_executor,
        publisher,
        telemetry,
        selected.memory_recall_max_results,
        approval_tool_codes=selected.approval_tool_codes,
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
    return ExecutionService(weave, repository, events, telemetry)
