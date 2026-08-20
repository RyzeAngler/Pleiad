"""Built-in main-Agent Tools backed by persistent Memory."""

from pleiad.contracts import JsonObject, JsonSchema
from pleiad.memory.model import MemoryPolicy
from pleiad.memory.store import MainAgentMemory
from pleiad.tools.base import FunctionTool
from pleiad.tools.contracts import ToolContext, ToolDescriptor

_MEMORY_RECALL_SCHEMA: JsonSchema = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "minLength": 1,
            "description": "What information is needed for the current task.",
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": 5,
        },
    },
    "required": ["query"],
    "additionalProperties": False,
}


def memory_recall_tool(
    memory: MainAgentMemory,
    *,
    conversation_id: str,
    policy: MemoryPolicy,
) -> FunctionTool:
    """Create a scope-bound, read-only Memory recall Tool for the main Agent."""
    calls = 0

    async def recall(context: ToolContext, arguments: JsonObject) -> JsonObject:
        nonlocal calls
        del context
        if calls >= policy.recall_max_calls_per_execution:
            return {
                "status": "limit_reached",
                "items": [],
                "message": "The Memory recall limit for this execution has been reached.",
            }

        calls += 1
        query = str(arguments["query"])
        requested_limit = int(arguments.get("limit", policy.recall_max_results))
        candidates = memory.recall_memory(
            conversation_id=conversation_id,
            query=query,
            limit=min(requested_limit, policy.recall_max_results),
            policy=policy,
        )
        remaining_chars = policy.recall_max_output_tokens * 4
        items: list[JsonObject] = []
        for item in candidates:
            if remaining_chars <= 0:
                break
            content = item.content[:remaining_chars]
            remaining_chars -= len(content)
            items.append(
                {
                    "memory_id": item.memory_id,
                    "scope": item.scope.value,
                    "kind": item.kind.value,
                    "content": content,
                    "truncated": len(content) < len(item.content),
                    "source_message_ids": list(item.source_message_ids),
                    "score": item.score,
                    "updated_at": item.updated_at.isoformat(),
                    "metadata": dict(item.metadata),
                }
            )
        return {
            "status": "ok",
            "items": items,
        }

    return FunctionTool(
        ToolDescriptor(
            name="memory_recall",
            code="pleiad.memory.recall",
            description="Retrieve relevant long-term Memory for the current task.",
        ),
        recall,
        _MEMORY_RECALL_SCHEMA,
    )
