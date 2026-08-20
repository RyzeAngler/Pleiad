"""Composition root for the Pleiad process."""

from loguru import logger

from pleiad.agent.factory import AgentFactory
from pleiad.agent.loop import AgentLoop
from pleiad.application.service import PleiadApplicationService
from pleiad.config import Settings
from pleiad.errors import ErrorCode, SystemError, make_error
from pleiad.graph.store import TaskGraphStore
from pleiad.graph.validator import GraphValidator
from pleiad.llm.errors import LLMError
from pleiad.logging import configure_logging
from pleiad.memory import JsonlMainAgentMemory, MemoryPolicy
from pleiad.models import create_model_registry
from pleiad.observability import TraceService, create_tracer
from pleiad.observability.stores.jsonl import JsonlTraceStore
from pleiad.runtime.capabilities import WorkspaceCapabilitySync
from pleiad.runtime.events import EventBus
from pleiad.runtime.executor import GraphExecutor
from pleiad.runtime.node_runner import NodeRunner
from pleiad.runtime.pleiad import PleiadAgent
from pleiad.skills.builtins import register_file_skills
from pleiad.skills.manifest import load_skill_descriptors
from pleiad.skills.zoo import SkillZoo
from pleiad.storage.artifacts import ArtifactStore
from pleiad.storage.repository import (
    ExecutionRepository,
    GraphRunRepository,
    NodeRunRepository,
)
from pleiad.tools.builtins import register_builtins
from pleiad.tools.manifest import load_tool_descriptors
from pleiad.tools.runtime import ToolRuntime
from pleiad.tools.workspace import register_workspace_tools
from pleiad.tools.zoo import ToolZoo


def create_application(settings: Settings | None = None) -> PleiadApplicationService:
    """Build all process-scoped registries and the API service."""
    selected = settings or Settings()
    selected.workspace.mkdir(parents=True, exist_ok=True)
    configure_logging(
        log_level=selected.log_level,
        log_file=selected.runtime_root / "logs" / "pleiad.{time:YYYY-MM-DD}.log",
        console=selected.log_console,
    )
    trace_service = TraceService(JsonlTraceStore(selected.runtime_root / "traces"))
    tracer = create_tracer(trace_service)
    try:
        logger.info("开始初始化 Pleiad Runtime")
        events = EventBus()
        tools = ToolZoo()
        register_builtins(tools, selected.workspace)
        register_workspace_tools(tools, selected.workspace)
        for descriptor in load_tool_descriptors(selected.resolved_capability_file):
            tools.register_descriptor(descriptor)

        skills = SkillZoo()
        skill_root = selected.workspace / "skills"
        skill_descriptors = load_skill_descriptors(selected.resolved_capability_file)
        register_file_skills(skills, skill_descriptors, skill_root)
        capability_sync = WorkspaceCapabilitySync(
            tools,
            skills,
            selected.workspace,
            selected.resolved_capability_file,
            events,
        )
        capability_sync.prime()
    except Exception as exc:
        logger.exception("能力目录初始化失败")
        raise SystemError(
            make_error(
                ErrorCode.SYSTEM_CAPABILITY,
                details={"exception": exc.__class__.__name__},
            )
        ) from exc
    try:
        models = create_model_registry(selected)
    except LLMError as exc:
        raise SystemError(
            make_error(
                ErrorCode.SYSTEM_CONFIGURATION,
                details={"llm_code": exc.code.value},
            )
        ) from exc
    except Exception as exc:
        logger.exception("模型注册表初始化失败")
        raise SystemError(
            make_error(
                ErrorCode.SYSTEM_CONFIGURATION,
                details={"exception": exc.__class__.__name__},
            )
        ) from exc
    graph_store = TaskGraphStore(selected.runtime_root / "graphs")
    validator = GraphValidator(tools, skills)
    artifacts = ArtifactStore(selected.runtime_root / "artifacts")
    memory = JsonlMainAgentMemory(
        selected.runtime_root / "memory",
        MemoryPolicy(
            user_id=selected.memory_user_id,
            cross_conversation_memory=selected.memory_cross_conversation,
            search_backend=selected.memory_search_backend,
            profile_max_items=selected.memory_profile_max_items,
            profile_max_tokens=selected.memory_profile_max_tokens,
            recall_max_calls_per_execution=selected.memory_recall_max_calls,
            recall_max_results=selected.memory_recall_max_results,
            recall_max_output_tokens=selected.memory_recall_max_output_tokens,
        ),
    )
    node_runner = NodeRunner(
        factory=AgentFactory(models, tools, skills),
        loop=AgentLoop(
            ToolRuntime(observability=tracer),
            events,
            observability=tracer,
            tools=tools,
            skills=skills,
            capability_sync=capability_sync,
        ),
        artifacts=artifacts,
        node_runs=NodeRunRepository(selected.runtime_root / "node-runs"),
        observability=tracer,
    )
    executor = GraphExecutor(
        validator=validator,
        runner=node_runner,
        events=events,
        graph_runs=GraphRunRepository(selected.runtime_root / "graph-runs"),
        observability=tracer,
        capability_sync=capability_sync,
    )
    pleiad = PleiadAgent(
        models,
        tools,
        skills,
        graph_store,
        executor,
        memory,
        events=events,
        observability=tracer,
        capability_sync=capability_sync,
    )
    logger.info("Pleiad Runtime 初始化完成")
    return PleiadApplicationService(
        pleiad,
        ExecutionRepository(selected.runtime_root / "executions"),
        tracer,
        trace_service,
    )
