import hashlib
import json
from typing import Literal

from pleiad.contracts import ContractModel, JsonObject, JsonValue
from pleiad.messages.results import ResultReference
from pleiad.sessions.input import InputEnvelope
from pleiad.tools.errors import ToolError
from pydantic import TypeAdapter

from pleiad_weave.infrastructure.storage.product_models import Attention, Execution, InboxItem, Page, Snapshot, Space

MODEL_INLINE_BYTES = 1_024
MODEL_INPUT_QUEUE_BYTES = 65_536
ROUTER_BATCH_SOURCE_BYTES = 131_072
MODEL_PAGE_ITEMS = 20
MODEL_SOURCE_PAGE_CHARS = 4_000
MAX_MODEL_SOURCE_PAGE_CHARS = 32_768
JSON_OBJECT = TypeAdapter[JsonObject](JsonObject)
type QueryTool = Literal["query_product", "query_work"]


def source_reference(kind: str, identity: str, content: str, *, tool: QueryTool) -> ResultReference:
    """Keep an exact source digest and executable bounded reading instructions."""
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return ResultReference(
        reference=f"weave:{kind}:{identity}",
        digest=digest,
        summary=json.dumps(
            {
                "total_chars": len(content),
                "preview": content[:128],
                "read": {
                    "tool": tool,
                    "arguments": {
                        "kind": kind,
                        "id": identity,
                        "start_char": 0,
                        "max_chars": MODEL_SOURCE_PAGE_CHARS,
                        "expected_digest": digest,
                    },
                },
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    )


def input_envelope(item: InboxItem) -> InputEnvelope:
    """Create the canonical SDK projection while preserving the original product input."""
    content: str | ResultReference = item.content
    if len(item.content.encode("utf-8")) > MODEL_INLINE_BYTES:
        content = source_reference(
            "input", item.input_id, item.content, tool="query_product" if item.target_kind == "router" else "query_work"
        )
    return InputEnvelope(
        input_id=item.input_id,
        kind=item.kind,
        source=item.source,
        content=content,
        causation_id=item.causation_id,
    )


def input_projection(item: InboxItem) -> JsonObject:
    """Project input provenance and bounded content without altering its stored bytes."""
    result = JSON_OBJECT.validate_python(item.model_dump(mode="json"))
    envelope = JSON_OBJECT.validate_python(input_envelope(item).model_dump(mode="json"))
    result["content"] = envelope["content"]
    return result


def record_projection(record: ContractModel, kind: str, identity: str, *, tool: QueryTool) -> JsonObject:
    """Keep small records inline and expose exact references for larger records."""
    serialized = record.model_dump_json()
    fields: set[str] | None = None
    if kind == "space":
        fields = set(Space.model_fields)
    elif kind == "execution":
        fields = set(Execution.model_fields)
    elif kind == "attention":
        fields = set(Attention.model_fields)
    content = record.model_dump_json(include=fields)
    value = JSON_OBJECT.validate_python(record.model_dump(mode="json"))
    if len(serialized.encode("utf-8")) <= MODEL_INLINE_BYTES:
        return value
    result: JsonObject = {
        f"{kind}_id": identity,
        "reference": source_reference(kind, identity, content, tool=tool).model_dump(mode="json"),
    }
    for key in (
        "revision",
        "status",
        "role",
        "created_at",
        "space_id",
        "execution_id",
        "parent_execution_id",
        "available_actions",
    ):
        if key in value:
            result[key] = value[key]
    for key in ("name", "objective", "question", "summary", "body"):
        text = value.get(key)
        if isinstance(text, str):
            result[f"{key}_preview"] = text[:80]
    return result


def page_projection[T: ContractModel](page: Page[T], kind: str, *, tool: QueryTool) -> JsonObject:
    """Retain pagination watermarks while bounding every individual source record."""
    result = JSON_OBJECT.validate_python(page.model_dump(mode="json", exclude={"items"}))
    items: list[JsonValue] = []
    for item in page.items:
        values = JSON_OBJECT.validate_python(item.model_dump(mode="json"))
        identity = values.get(f"{kind}_id")
        if not isinstance(identity, str):
            raise ValueError("A projected source requires its exact persisted identity")
        items.append(record_projection(item, kind, identity, tool=tool))
    result["items"] = items
    return result


def snapshot_projection(snapshot: Snapshot) -> JsonObject:
    """Build a model-sized product snapshot with retrievable original records."""
    return {
        "snapshot_sequence": snapshot.snapshot_sequence,
        "routing": snapshot.routing.model_dump(mode="json"),
        "messages": page_projection(snapshot.messages, "message", tool="query_product"),
        "spaces": page_projection(snapshot.spaces, "space", tool="query_product"),
        "executions": page_projection(snapshot.executions, "execution", tool="query_product"),
        "attentions": page_projection(snapshot.attentions, "attention", tool="query_product"),
        "capabilities": snapshot.capabilities,
    }


def source_page(
    kind: str,
    identity: str,
    content: str,
    *,
    start_char: int,
    max_chars: int,
    expected_digest: str | None,
    tool: QueryTool,
) -> JsonObject:
    """Read an exact authorized source window and detect changed source versions."""
    reference = source_reference(kind, identity, content, tool=tool)
    if expected_digest is not None and expected_digest != reference.digest:
        raise ToolError("weave.source_changed", "The source changed. Read its current reference before continuing.")
    if start_char > len(content):
        raise ToolError("weave.source_range_invalid", "The requested character offset exceeds the source length.")
    end = min(start_char + max_chars, len(content))
    return {
        "reference": reference.model_dump(mode="json"),
        "total_chars": len(content),
        "start_char": start_char,
        "next_start_char": end,
        "content": content[start_char:end],
        "truncated": end < len(content),
    }
