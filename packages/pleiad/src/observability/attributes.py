from collections.abc import Mapping, Sequence

SCOPE_NAME = "pleiad"

RUN_NAME = "pleiad.run"
TURN_NAME = "pleiad.turn"
CONTEXT_BUILD_NAME = "pleiad.context.build"
CONTEXT_COMPACT_NAME = "pleiad.context.compact"
MODEL_GENERATE_NAME = "pleiad.model.generate"
TOOL_EXECUTE_NAME = "pleiad.tool.execute"
MCP_CALL_NAME = "pleiad.mcp.call"
SANDBOX_EXECUTE_NAME = "pleiad.sandbox.execute"
SANDBOX_CREATE_NAME = "pleiad.sandbox.create"
SANDBOX_CLEANUP_NAME = "pleiad.sandbox.cleanup"
GRAPH_EXECUTE_NAME = "pleiad.graph.execute"
GRAPH_NODE_NAME = "pleiad.graph.node"
MEMORY_READ_NAME = "pleiad.memory.read"

RUN_ID = "pleiad.run.id"
PARENT_RUN_ID = "pleiad.parent_run.id"
TURN_ID = "pleiad.turn.id"
CALL_ID = "pleiad.call.id"
AGENT_NAME = "pleiad.agent.name"
TOOL_CODE = "pleiad.tool.code"
TOOL_NAME = "pleiad.tool.name"
GRAPH_ID = "pleiad.graph.id"
NODE_ID = "pleiad.node.id"
PROVIDER = "gen_ai.provider.name"
MODEL = "gen_ai.request.model"
PROTOCOL = "pleiad.model.protocol"
OPERATION = "gen_ai.operation.name"
FINISH_REASON = "gen_ai.response.finish_reasons"
INPUT_TOKENS = "gen_ai.usage.input_tokens"
OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
TOTAL_TOKENS = "pleiad.model.usage.total_tokens"
REASONING_TOKENS = "pleiad.model.usage.reasoning_tokens"
CACHED_INPUT_TOKENS = "pleiad.model.usage.cached_input_tokens"
STATUS = "pleiad.status"
ERROR_CODE = "pleiad.error.code"
ERROR_TYPE = "pleiad.error.type"
FIRST_OUTPUT_MS = "pleiad.model.first_output_ms"
PRIVACY_MASKED = "pleiad.privacy.masked"
SANDBOX_BACKEND = "pleiad.sandbox.backend"
SANDBOX_TIMED_OUT = "pleiad.sandbox.timed_out"
SANDBOX_EXIT_CODE = "pleiad.sandbox.exit_code"

AttributeValue = bool | int | float | str | Sequence[str] | Sequence[bool] | Sequence[int] | Sequence[float]
AttributeMap = Mapping[str, AttributeValue]


def attribute_values(values: Mapping[str, object]) -> dict[str, AttributeValue]:
    """Keep only OpenTelemetry-compatible scalar and sequence attributes."""
    result: dict[str, AttributeValue] = {}
    for name, value in values.items():
        if isinstance(value, bool | int | float | str):
            result[name] = value
        elif isinstance(value, list | tuple) and value:
            if all(isinstance(item, str) for item in value):
                result[name] = tuple(item for item in value if isinstance(item, str))
            elif all(type(item) is bool for item in value):
                result[name] = tuple(item for item in value if type(item) is bool)
            elif all(type(item) is int for item in value):
                result[name] = tuple(item for item in value if type(item) is int)
            elif all(type(item) is float for item in value):
                result[name] = tuple(item for item in value if type(item) is float)
    return result
