"""PleiadAgent supervisor and its graph-control Tools."""

import json
from threading import RLock

from pydantic import ValidationError

from pleiad.agent.contracts import AgentContext, AgentDefinition, AgentPolicy, AgentResult
from pleiad.agent.loop import AgentLoop
from pleiad.agent.prompt import PLEIAD_SYSTEM_PROMPT
from pleiad.config import ModelRole
from pleiad.context import ContextBuilder, ContextRenderer, ContextRequest
from pleiad.contracts import JsonObject, JsonSchema
from pleiad.graph.contracts import GraphSpec
from pleiad.graph.store import GraphConflictError, TaskGraphStore
from pleiad.graph.validator import GraphValidator
from pleiad.memory import MainAgentMemory, MemoryPolicy
from pleiad.memory.tool import memory_recall_tool
from pleiad.models import ModelRegistry
from pleiad.observability import ObservabilityPort
from pleiad.runtime.capabilities import WorkspaceCapabilitySync
from pleiad.runtime.events import EventBus
from pleiad.runtime.executor import GraphExecutor
from pleiad.skills.zoo import SkillZoo
from pleiad.tools.base import FunctionTool
from pleiad.tools.contracts import ToolContext, ToolDescriptor
from pleiad.tools.runtime import ToolRuntime
from pleiad.tools.zoo import ToolZoo

_TOOL_DEVELOPMENT_SKILL_CODE = "pleiad.skill.tool-development"


class PleiadAgent:
    """Use a minimal Agent loop to search, generate, and execute TaskGraphs."""

    def __init__(
        self,
        models: ModelRegistry,
        tools: ToolZoo,
        skills: SkillZoo,
        graph_store: TaskGraphStore,
        graph_executor: GraphExecutor,
        memory: MainAgentMemory,
        *,
        events: EventBus | None = None,
        observability: ObservabilityPort | None = None,
        capability_sync: WorkspaceCapabilitySync | None = None,
    ) -> None:
        self.models = models
        self.tools = tools
        self.skills = skills
        self.graph_store = graph_store
        self.graph_executor = graph_executor
        self.memory = memory
        self.memory_policy = getattr(memory, "policy", MemoryPolicy())
        self.events = events
        self.observability = observability
        self.capability_sync = capability_sync
        self.context_builder = ContextBuilder()
        self.context_renderer = ContextRenderer()
        self.validator = GraphValidator(tools, skills)
        self.tool_runtime = ToolRuntime(observability=observability)
        self.loop = AgentLoop(
            self.tool_runtime,
            events,
            observability=observability,
            tools=tools,
            skills=skills,
            capability_sync=capability_sync,
        )
        self._execution_graphs: dict[str, tuple[str, int]] = {}
        self._execution_graphs_lock = RLock()

    async def run(
        self,
        objective: str,
        business_input: JsonObject,
        *,
        conversation_id: str,
        execution_id: str,
    ) -> AgentResult:
        """Run the Pleiad supervisor for one user objective."""
        turn = self.memory.begin_turn(
            conversation_id,
            execution_id,
            objective,
        )
        binding = self.models.resolve(ModelRole.TEXT)
        definition = AgentDefinition(
            agent_id="pleiad-supervisor",
            model_role="text",
            client=binding.client,
            prompt=PLEIAD_SYSTEM_PROMPT,
            tools=(
                *self.tools.builtin_tools(),
                memory_recall_tool(
                    self.memory,
                    conversation_id=conversation_id,
                    policy=self.memory_policy,
                ),
                *self._control_tools(execution_id),
            ),
            policy=AgentPolicy(timeout_seconds=1_800),
            dynamic_skill_codes=self._supervisor_skill_codes(objective),
        )
        context_request = ContextRequest(
            agent_id="pleiad-supervisor",
            conversation_id=conversation_id,
            execution_id=execution_id,
            raw_query=objective,
            business_input=business_input,
            personal_profile=turn.personal_profile,
            checkpoint_handoff=turn.checkpoint_handoff,
        )
        user_message = self.context_renderer.render(
            context_request,
            self.context_builder.build(context_request),
        )
        result = await self.loop.run(
            definition,
            user_message,
            execution_id=execution_id,
            context=AgentContext(
                messages=turn.history,
                conversation_id=conversation_id,
                raw_query=objective,
                checkpoint_handoff=turn.checkpoint_handoff,
                history_sequence=turn.history_sequence,
                checkpoint_sink=turn.append_checkpoint,
            ),
            message_sink=turn.append_model_message,
        )
        self.memory.finish_turn(
            turn,
            _visible_result(result),
            succeeded=result.status.value == "succeeded",
        )
        return result

    def _supervisor_skill_codes(self, objective: str) -> tuple[str, ...]:
        """Select the built-in Tool development Skill for capability-building goals."""
        normalized = objective.casefold()
        tool_terms = ("tool", "工具", "能力")
        development_terms = ("开发", "实现", "新增", "自定义", "编写", "创建", "扩展")
        if not any(term in normalized for term in tool_terms):
            return ()
        if not any(term in normalized for term in development_terms):
            return ()
        try:
            self.skills.resolve(_TOOL_DEVELOPMENT_SKILL_CODE)
        except LookupError:
            return ()
        return (_TOOL_DEVELOPMENT_SKILL_CODE,)

    def _control_tools(self, execution_id: str) -> tuple[FunctionTool, ...]:
        """Create ordinary BaseTool wrappers for supervisor operations."""
        return (
            FunctionTool(
                ToolDescriptor(
                    name="pleiad_search_capabilities",
                    code="pleiad.system.search_capabilities",
                    description="搜索当前 Tool 和 Skill 的名称、代码和描述",
                ),
                self._search,
                _search_schema(),
            ),
            FunctionTool(
                ToolDescriptor(
                    name="pleiad_submit_graph",
                    code="pleiad.system.submit_graph",
                    description="提交一个通过结构校验的动态任务图 revision",
                ),
                self._submit_graph,
                _submit_schema(),
            ),
            FunctionTool(
                ToolDescriptor(
                    name="pleiad_execute_graph",
                    code="pleiad.system.execute_graph",
                    description="执行当前任务图并返回结构化节点结果",
                ),
                self._execute_graph,
                _execute_schema(),
            ),
            FunctionTool(
                ToolDescriptor(
                    name="pleiad_inspect_graph",
                    code="pleiad.system.inspect_graph",
                    description="查看当前任务图的节点、边和 revision",
                ),
                self._inspect_graph,
                _inspect_schema(),
            ),
        )

    async def _search(self, context: ToolContext, arguments: JsonObject) -> JsonObject:
        capability_sync = getattr(self, "capability_sync", None)
        if capability_sync:
            capability_sync.sync(execution_id=context.execution_id)
        query = str(arguments.get("query", ""))
        kind = str(arguments.get("kind", "all"))
        limit = int(arguments.get("limit", 20))
        tools = self.tools.search(query, limit) if kind in {"all", "tool"} else ()
        skills = self.skills.search(query, limit) if kind in {"all", "skill"} else ()
        return {
            "tools": [descriptor.model_dump(mode="json") for descriptor in tools],
            "skills": [descriptor.model_dump(mode="json") for descriptor in skills],
        }

    async def _submit_graph(self, context: ToolContext, arguments: JsonObject) -> JsonObject:
        capability_sync = getattr(self, "capability_sync", None)
        if capability_sync:
            capability_sync.sync(execution_id=context.execution_id)
        expected = arguments.get("expected_revision")
        raw_graph = {key: value for key, value in arguments.items() if key != "expected_revision"}
        try:
            graph = GraphSpec.model_validate(raw_graph)
        except ValidationError as exc:
            diagnostics = _graph_structure_diagnostics(exc)
            self.events and self.events.emit(
                context.execution_id,
                "graph.revision.rejected",
                {"diagnostics": diagnostics},
            )
            return {"accepted": False, "diagnostics": diagnostics}
        validation = self.validator.validate(graph)
        if not validation.valid:
            self.events and self.events.emit(
                context.execution_id,
                "graph.revision.rejected",
                {
                    "graph_id": graph.graph_id,
                    "diagnostics": [
                        item.model_dump(mode="json") for item in validation.diagnostics
                    ],
                },
            )
            return {
                "accepted": False,
                "diagnostics": [item.model_dump(mode="json") for item in validation.diagnostics],
            }
        try:
            stored = self.graph_store.submit(graph, int(expected) if expected is not None else None)
        except GraphConflictError as exc:
            self.events and self.events.emit(
                context.execution_id,
                "graph.revision.rejected",
                {"graph_id": graph.graph_id, "reason": str(exc)},
            )
            return {"accepted": False, "conflict": str(exc)}
        with self._execution_graphs_lock:
            self._execution_graphs[context.execution_id] = (
                stored.graph.graph_id,
                stored.graph.revision,
            )
        self.events and self.events.emit(
            context.execution_id,
            "graph.revision.accepted",
            {
                "graph_id": stored.graph.graph_id,
                "revision": stored.graph.revision,
                "nodes": [
                    {"node_id": node.node_id, "objective": node.objective}
                    for node in stored.graph.nodes
                ],
                "edges": [
                    {
                        "edge_id": edge.edge_id,
                        "source_node_id": edge.source_node_id,
                        "target_node_id": edge.target_node_id,
                    }
                    for edge in stored.graph.edges
                ],
            },
        )
        return {
            "accepted": True,
            "graph_id": stored.graph.graph_id,
            "revision": stored.graph.revision,
        }

    def graph_for_execution(self, execution_id: str) -> tuple[str, int] | None:
        """Return the graph revision submitted during one supervisor run."""
        with self._execution_graphs_lock:
            return self._execution_graphs.get(execution_id)

    async def _execute_graph(self, context: ToolContext, arguments: JsonObject) -> JsonObject:
        graph_id = str(arguments["graph_id"])
        raw_input = arguments.get("input", {})
        if not isinstance(raw_input, dict):
            raise ValueError("input must be an object")
        stored = self.graph_store.current(graph_id)
        if stored is None:
            raise LookupError(f"graph not found: {graph_id}")
        result = await self.graph_executor.execute(
            stored.graph,
            raw_input,
            execution_id=context.execution_id,
        )
        return result.model_dump(mode="json")

    async def _inspect_graph(self, context: ToolContext, arguments: JsonObject) -> JsonObject:
        graph_id = str(arguments["graph_id"])
        stored = self.graph_store.current(graph_id)
        if stored is None:
            raise LookupError(f"graph not found: {graph_id}")
        return stored.graph.model_dump(mode="json")


def _search_schema() -> JsonSchema:
    return {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "kind": {"type": "string", "enum": ["all", "tool", "skill"]},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
        },
        "additionalProperties": False,
    }


def _submit_schema() -> JsonSchema:
    schema: JsonSchema = GraphSpec.model_json_schema()
    properties = schema.setdefault("properties", {})
    if isinstance(properties, dict):
        properties["expected_revision"] = {
            "type": "integer",
            "minimum": 1,
            "description": "更新已有任务图时填写当前 revision, 首次提交省略",
        }
    return schema


def _execute_schema() -> JsonSchema:
    return {
        "type": "object",
        "properties": {"graph_id": {"type": "string"}, "input": {"type": "object"}},
        "required": ["graph_id"],
        "additionalProperties": False,
    }


def _inspect_schema() -> JsonSchema:
    return {
        "type": "object",
        "properties": {"graph_id": {"type": "string"}},
        "required": ["graph_id"],
        "additionalProperties": False,
    }


def _graph_structure_diagnostics(exc: ValidationError) -> list[JsonObject]:
    """Return safe, actionable Graph structure findings to Pleiad."""
    return [
        {
            "code": str(error["type"]),
            "message": str(error["msg"]),
            "path": ".".join(str(part) for part in error["loc"]),
        }
        for error in exc.errors(
            include_url=False,
            include_context=False,
            include_input=False,
        )
    ]


def _visible_result(result: AgentResult) -> str:
    """Project only the user-visible result into the durable Conversation log."""
    if result.text:
        return result.text
    if isinstance(result.structured_output, dict):
        return json.dumps(result.structured_output, ensure_ascii=False)
    return ""
