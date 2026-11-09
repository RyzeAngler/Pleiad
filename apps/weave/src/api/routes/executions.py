import asyncio
import hashlib
from pathlib import PurePosixPath
from typing import Literal

from fastapi import APIRouter, Depends, Query
from pleiad.contracts import JsonObject, JsonValue
from pleiad.messages.message import ToolMessage
from pleiad.messages.results import OperationResultReference, ToolResultReference
from pleiad.sessions.records import RunSnapshot
from pleiad.sessions.state import SessionState
from pleiad.tools.contracts import ToolResult
from pydantic import TypeAdapter, ValidationError

from pleiad_weave.api.dependencies import get_service, get_writable_service
from pleiad_weave.api.models import DeliveryRetryRequest, RetryRequest, RevisionRequest
from pleiad_weave.application.service import ExecutionService
from pleiad_weave.infrastructure.storage.product_details import GraphRunDetail, NodeAttempt, text_page
from pleiad_weave.infrastructure.storage.product_models import (
    Delivery,
    ExecutionRun,
    ExecutionRunPage,
    ExecutionStatus,
    ExecutionView,
    Page,
)

router = APIRouter(prefix="/v1")
service_dependency = Depends(get_service)
write_dependency = Depends(get_writable_service)
JSON_VALUE: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)


def readable_fields(value: JsonValue) -> str:
    """Render business data without discarding unknown fields or identifiers."""
    if isinstance(value, dict):
        labels = {
            "name": "名称",
            "description": "说明",
            "title": "标题",
            "path": "文件",
            "url": "链接",
            "content": "内容",
            "text": "正文",
            "summary": "摘要",
            "items": "内容",
            "results": "结果",
            "sources": "来源",
            "status": "状态",
            "reason": "原因",
            "message": "说明",
            "size": "文件大小 / 字节",
            "bytes_written": "已写入字节数",
            "count": "数量",
            "total": "总数",
            "query": "查询内容",
            "stdout": "输出",
            "stderr": "错误输出",
            "exit_code": "退出码",
            "error_message": "失败原因",
            "warning": "提醒",
            "warnings": "提醒",
            "line_start": "起始行",
            "line_end": "结束行",
            "purpose": "用途",
            "instructions": "工作要求",
            "objective": "任务要求",
            "result": "结果",
            "body": "消息正文",
            "state": "处理状态",
            "execution": "任务",
            "inputs": "收到的内容",
            "messages": "相关消息",
            "responsibilities": "处理情况",
            "objective_preview": "任务要求节选",
            "body_preview": "消息正文节选",
            "required": "需要完成",
        }
        lines = []
        for key, item in value.items():
            if item is None:
                continue
            content = readable_fields(item)
            if content:
                lines.append(f"{labels.get(key, key.replace('_', ' '))}\n{content}")
        return "\n\n".join(lines)
    if isinstance(value, list):
        return "\n\n".join(f"• {content}" for item in value if (content := readable_fields(item)))
    if isinstance(value, bool):
        return "是" if value else "否"
    if value is None:
        return ""
    return str(value)


def display_product_record(record: JsonObject, kind: str) -> JsonObject:
    """Select user-facing fields from a known product record or bounded preview."""
    fields = {
        "space": ("name", "purpose", "instructions"),
        "execution": ("objective", "objective_preview", "status", "reason", "result"),
        "message": ("body", "body_preview"),
        "disposition": ("state", "required"),
    }
    statuses = {
        "queued": "排队中",
        "running": "进行中",
        "waiting_input": "等待补充",
        "waiting_approval": "等待批准",
        "waiting_children": "等待子任务",
        "blocked": "受阻",
        "paused": "已暂停",
        "cancelling": "正在取消",
        "cancelled": "已取消",
        "succeeded": "已完成",
        "failed": "执行失败",
        "owned": "由当前任务处理",
        "handled": "已处理",
        "returned": "已转交",
        "resource_unavailable": "资源不可用",
        "unhandled_failure": "执行失败",
        "recovery_unknown": "结果待核实",
    }
    visible: JsonObject = {}
    for key in fields[kind]:
        item = record.get(key)
        if isinstance(item, str):
            visible[key] = statuses.get(item, item) if key in {"status", "state", "reason"} else item
        elif isinstance(item, bool):
            visible[key] = item
    return visible


def display_product_source(value: JsonObject, reference: str) -> str:
    """Render known product source records without guessing incomplete JSON."""
    content = value["content"]
    if not isinstance(content, str):
        raise ValueError("The product source has no text content")
    kind = reference.split(":", 2)[1]
    if kind not in {"space", "execution", "message", "disposition"}:
        return content
    if value.get("truncated") is True or value.get("start_char") != 0:
        return "资料记录不完整。"
    try:
        record = JSON_VALUE.validate_json(content)
    except ValidationError as error:
        raise ValueError("The stored product source is not a JSON record") from error
    if not isinstance(record, dict):
        raise ValueError("The stored product source is not an object")
    return readable_fields(display_product_record(record, kind))


def display_work_overview(value: JsonObject) -> str:
    """Project the owned query_work overview without exposing reference hints."""
    execution = value["execution"]
    if not isinstance(execution, dict):
        raise ValueError("The work overview has no execution record")
    visible: JsonObject = {"execution": display_product_record(execution, "execution")}
    for key, kind in (("messages", "message"), ("responsibilities", "disposition")):
        records = value[key]
        if not isinstance(records, list) or any(not isinstance(record, dict) for record in records):
            raise ValueError(f"The work overview has invalid {key}")
        visible[key] = [display_product_record(record, kind) for record in records if isinstance(record, dict)]
    inputs = value["inputs"]
    if not isinstance(inputs, list):
        raise ValueError("The work overview has invalid inputs")
    contents: list[JsonValue] = []
    for item in inputs:
        if not isinstance(item, dict):
            raise ValueError("The work overview has an invalid input")
        content = item.get("content")
        if isinstance(content, str):
            contents.append(content)
        elif isinstance(content, dict) and isinstance(content.get("reference"), str):
            contents.append("输入正文未包含在本次结果中。")
        else:
            raise ValueError("The work overview input has no content")
    visible["inputs"] = contents
    for key, label in (
        ("total_inputs", "收到的内容总数"),
        ("total_messages", "相关消息总数"),
        ("total_responsibilities", "处理记录总数"),
    ):
        visible[label] = value[key]
    results = value["tool_results"]
    if not isinstance(results, list):
        raise ValueError("The work overview has invalid tool results")
    visible["本次读取的工具结果数"] = len(results)
    content = readable_fields(visible)
    return content + ("\n\n还有更多记录。" if value["next_cursor"] is not None else "")


def display_result_page(value: JsonObject, reference: JsonObject) -> tuple[str, Literal["text", "markdown", "code"]]:
    """Unwrap complete SDK result pages and retain incomplete-read facts.

    A bounded inner JSON fragment cannot be interpreted as a complete result.
    Digest and character positions apply to the original persisted text, before
    any presentation conversion or UTF-8 response pagination.
    """
    parsed = (
        OperationResultReference.model_validate(reference)
        if "operation_id" in reference
        else ToolResultReference.model_validate(reference)
    )
    content, start, end = value["content"], value["start_char"], value["next_start_char"]
    if (
        not isinstance(content, str)
        or not isinstance(start, int)
        or isinstance(start, bool)
        or not isinstance(end, int)
        or isinstance(end, bool)
        or not 0 <= start <= end <= parsed.total_chars
        or end - start != len(content)
        or value["truncated"] is not (end < parsed.total_chars)
    ):
        raise ValueError("The stored SDK result page is inconsistent")
    if start != 0 or value["truncated"]:
        return "结果记录不完整。" + ("\n\n内容已截断。" if value["truncated"] else ""), "text"
    if hashlib.sha256(content.encode("utf-8")).hexdigest() != parsed.sha256:
        raise ValueError("The stored SDK result page does not match its digest")
    if isinstance(parsed, OperationResultReference):
        ToolResult.model_validate_json(content)
    return display_tool_observation(content)


def display_tool_observation(text: str) -> tuple[str, Literal["text", "markdown", "code"]]:
    """Extract readable tool content before paging while retaining error facts.

    Plain observations are a supported tool contract. Structured SDK envelopes
    must have a valid success flag and the corresponding output or error text.
    """
    try:
        value = JSON_VALUE.validate_json(text)
    except ValidationError:
        return text, "text"
    if isinstance(value, dict) and "ok" in value:
        if not isinstance(value["ok"], bool):
            raise ValueError("The stored tool result has an invalid success flag")
        if not value["ok"]:
            error = value.get("error_message")
            if not isinstance(error, str) or not error:
                raise ValueError("The failed tool result has no error description")
            output = value.get("output")
            if output is not None and not isinstance(output, dict):
                raise ValueError("The failed tool result has an invalid partial output")
            details = readable_fields(output)
            return f"操作未完成\n\n{error}" + (f"\n\n{details}" if details else ""), "text"
        output = value.get("output")
        if not isinstance(output, dict):
            raise ValueError("The successful tool result has no object output")
        value = output
    if isinstance(value, dict):
        overview_fields = {
            "execution",
            "inputs",
            "messages",
            "next_cursor",
            "responsibilities",
            "tool_results",
            "total_inputs",
            "total_messages",
            "total_responsibilities",
        }
        execution = value.get("execution")
        if value.keys() == overview_fields and isinstance(execution, dict) and "execution_id" in execution:
            return display_work_overview(value), "text"
        if value.get("status") == "started" and isinstance(value.get("operation_id"), str):
            return "操作已启动", "text"
        if "stdout" in value or "stderr" in value:
            stdout, stderr, exit_code = value.get("stdout"), value.get("stderr"), value.get("exit_code")
            if (
                not isinstance(stdout, str)
                or not isinstance(stderr, str)
                or not isinstance(exit_code, int)
                or isinstance(exit_code, bool)
            ):
                raise ValueError("The stored command result is incomplete")
            parts = [f"命令已结束。退出码 {exit_code}。"]
            if stdout:
                parts.append(f"输出\n{stdout}")
            if stderr:
                parts.append(f"错误输出\n{stderr}")
            if value.get("timed_out") is True:
                parts.append("命令执行超时。")
            if value.get("output_truncated") is True:
                parts.append("输出已截断。")
            if value.get("decoding_loss") is True:
                parts.append("输出包含无法识别的字符。")
            return "\n\n".join(parts), "code"
        if {"path", "bytes_written", "sha256"} <= value.keys():
            return readable_fields({key: item for key, item in value.items() if key != "sha256"}), "text"
        content = value.get("content")
        if isinstance(content, str):
            omitted = {"content"}
            reference = value.get("reference")
            if (
                isinstance(reference, dict)
                and reference.keys()
                in (OperationResultReference.model_fields.keys(), ToolResultReference.model_fields.keys())
                and value.keys() == {"reference", "content", "start_char", "next_start_char", "truncated"}
            ):
                return display_result_page(value, reference)
            source_reference = reference.get("reference") if isinstance(reference, dict) else None
            source_page = (
                isinstance(source_reference, str)
                and source_reference.startswith("weave:")
                and {"start_char", "next_start_char", "total_chars", "truncated"} <= value.keys()
            )
            file_content = {"path", "line_start", "line_end", "sha256", "truncated"} <= value.keys()
            if source_page:
                omitted.update({"reference", "start_char", "next_start_char", "total_chars", "truncated"})
                if not isinstance(source_reference, str):
                    raise ValueError("The product source has no reference")
                content = display_product_source(value, source_reference)
            if file_content:
                omitted.update({"sha256", "truncated"})
            details = readable_fields({key: item for key, item in value.items() if key not in omitted})
            if details:
                content = f"{details}\n\n{content}"
            if (source_page or file_content) and value.get("truncated") is True:
                content += "\n\n内容已截断。"
            path = value.get("path")
            if isinstance(path, str) and PurePosixPath(path).suffix.lower() not in {".md", ".markdown"}:
                return content, "text"
            return content, "markdown"
    content = readable_fields(value)
    return (content if content else "此操作未返回可展示的正文。"), "text"


def run_owner(service: ExecutionService, execution_id: str, run_id: str) -> tuple[str, ExecutionRun | NodeAttempt]:
    """Resolve only SDK Run identities actually owned by this product Execution."""
    with service.store.transaction() as connection:
        execution = service.details.require_execution(connection, execution_id)
        runs = service.store.records(connection, "execution_runs", ExecutionRun)
        for value in runs:
            if value.execution_id == execution_id and value.run_id == run_id:
                return execution.session_id, value
        for attempt in service.store.records(connection, "node_attempts", NodeAttempt):
            if attempt.execution_id == execution_id and attempt.sdk_run_id == run_id and attempt.session_id is not None:
                return attempt.session_id, attempt
    raise LookupError("The SDK Run does not belong to this Execution")


async def sdk_lineage(service: ExecutionService, execution_id: str) -> tuple[set[str], dict[str, RunSnapshot]]:
    """Read exact root and descendant SDK identities owned by one product Execution."""
    execution = await service.require_execution(execution_id)
    root = await service.sessions.snapshot(execution.session_id)
    roots = {value.run_id for value in root.runs}
    snapshots = {value.run_id: value for value in root.runs}
    for session_id in await service.sessions.descendant_sessions(tuple(roots)):
        snapshots.update((value.run_id, value) for value in (await service.sessions.snapshot(session_id)).runs)
    return roots, snapshots


def root_run_identity(parent_run_id: str, roots: set[str], snapshots: dict[str, RunSnapshot]) -> str:
    """Resolve a graph's actual root SDK invocation without inferring it from names."""
    current = parent_run_id
    visited: set[str] = set()
    while current not in roots:
        if current in visited:
            raise LookupError("The graph's SDK ancestry is cyclic")
        visited.add(current)
        snapshot = snapshots.get(current)
        if snapshot is None or snapshot.parent_run_id is None:
            raise LookupError("The graph's parent Run is outside this Execution")
        current = snapshot.parent_run_id
    return current


def graph_metadata(value: GraphRunDetail) -> JsonObject:
    """Project recovery metadata while output bodies remain behind owned content reads."""
    return value.model_dump(
        mode="json",
        exclude={
            "result": True,
            "input": True,
            "state": {"node_states": {"__all__": {"output", "input_value", "checkpoint"}}},
        },
    )


@router.get("/executions")
async def executions(
    space_id: str | None = None,
    status: ExecutionStatus | None = None,
    cursor: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
    service: ExecutionService = service_dependency,
) -> Page[ExecutionView]:
    """Read bounded real jobs independently of user chat identity."""
    return await asyncio.to_thread(
        service.store.list_executions, space_id=space_id, status=status, cursor=cursor, limit=limit
    )


@router.get("/executions/{execution_id}")
async def execution(execution_id: str, service: ExecutionService = service_dependency) -> JsonObject:
    """Read current durable status, action eligibility, and source records."""
    return await service.execution_detail(execution_id)


@router.post("/executions/{execution_id}/pause", status_code=202)
async def pause(
    execution_id: str, request: RevisionRequest, service: ExecutionService = write_dependency
) -> JsonObject:
    """Persist a pause intent before its runtime reaches a safe checkpoint."""
    await service.control(
        execution_id, "pause", request_id=request.request_id, expected_revision=request.expected_revision
    )
    return await service.execution_detail(execution_id)


@router.post("/executions/{execution_id}/cancel", status_code=202)
async def cancel(
    execution_id: str, request: RevisionRequest, service: ExecutionService = write_dependency
) -> JsonObject:
    """Persist cancellation and let the application owner coordinate cleanup."""
    await service.control(
        execution_id, "cancel", request_id=request.request_id, expected_revision=request.expected_revision
    )
    return await service.execution_detail(execution_id)


@router.post("/executions/{execution_id}/resume", status_code=202)
async def resume(
    execution_id: str, request: RevisionRequest, service: ExecutionService = write_dependency
) -> JsonObject:
    """Request current authorization and checkpoint validation through the owner."""
    await service.control(
        execution_id, "resume", request_id=request.request_id, expected_revision=request.expected_revision
    )
    return await service.execution_detail(execution_id)


@router.post("/executions/{execution_id}/retry", status_code=202)
async def retry(execution_id: str, request: RetryRequest, service: ExecutionService = write_dependency) -> JsonObject:
    """Create a new root job while retaining the source's terminal facts."""
    value = await service.retry(
        execution_id,
        request_id=request.request_id,
        expected_revision=request.expected_revision,
        supplement=request.supplement,
    )
    return {"new_execution_id": value.execution_id, "execution": await service.execution_detail(value.execution_id)}


@router.post("/deliveries/{delivery_id}/retry", status_code=202)
async def retry_delivery(
    delivery_id: str, request: DeliveryRetryRequest, service: ExecutionService = write_dependency
) -> Delivery:
    """Reschedule only the original generation-checked delivery content."""
    return await service.retry_delivery(
        delivery_id, request_id=request.request_id, expected_generation=request.expected_generation
    )


@router.get("/executions/{execution_id}/runs")
async def runs(
    execution_id: str,
    before_sequence: int | None = Query(default=None, ge=1),
    limit: int = Query(default=50, ge=1, le=100),
    service: ExecutionService = service_dependency,
) -> ExecutionRunPage:
    """Read newest SDK invocations with a stable boundary for older attempts."""
    return await asyncio.to_thread(
        service.store.execution_run_page, execution_id, before_sequence=before_sequence, limit=limit
    )


@router.get("/executions/{execution_id}/runs/{run_id}")
async def run(
    execution_id: str,
    run_id: str,
    before_sequence: int | None = Query(default=None, ge=1),
    limit: int = Query(default=50, ge=1, le=100),
    service: ExecutionService = service_dependency,
) -> JsonObject:
    """Read owned SDK steps without returning unbounded original tool observations."""
    session_id, record = await asyncio.to_thread(run_owner, service, execution_id, run_id)
    state: SessionState = await service.sessions.snapshot(session_id)
    snapshot = next((value for value in state.runs if value.run_id == run_id), None)
    if snapshot is None:
        raise LookupError("The accepted SDK Run is unavailable in its Session")
    owned_steps = tuple(value for value in reversed(state.steps) if value.run_id == run_id)
    selected = tuple(value for value in owned_steps if before_sequence is None or value.run_sequence < before_sequence)
    page_steps = selected[:limit]
    steps: list[JsonValue] = []
    references: list[JsonValue] = []
    for step in page_steps:
        messages: list[JsonValue] = []
        for message in step.messages:
            projected = message.model_dump(mode="json")
            if isinstance(message, ToolMessage):
                identity = f"{run_id}/{message.message_id}"
                projected["content"] = "Original tool observation is available through its owned result reference"
                references.append(
                    {
                        "kind": "tool_message",
                        "tool_result_id": identity,
                        "step_id": step.step_id,
                        "call_id": message.tool_call_id,
                    }
                )
            messages.append(projected)
        steps.append({**step.model_dump(mode="json", exclude={"messages"}), "messages": messages})
    selected_ids = {step.step_id for step in page_steps}
    calls = {value.call_id: value for value in state.tool_calls if value.run_id == run_id}
    operations: list[JsonValue] = []
    for operation in state.operations:
        call = calls.get(operation.call_id)
        if operation.run_id != run_id or call is None or call.step_id not in selected_ids:
            continue
        operations.append(
            {
                "operation_id": operation.operation_id,
                "call_id": operation.call_id,
                "step_id": call.step_id,
                "state": operation.state,
            }
        )
        if operation.result is None:
            continue
        references.append(
            {
                "kind": "operation",
                "operation_id": operation.operation_id,
                "state": operation.state,
                "tool_result_id": f"operation/{run_id}/{operation.operation_id}",
                "step_id": call.step_id,
                "call_id": operation.call_id,
            }
        )
    run_value: JsonObject = (
        record.model_dump(mode="json")
        if isinstance(record, ExecutionRun)
        else {
            "execution_id": execution_id,
            "attempt": record.attempt,
            "invocation_id": snapshot.invocation_id,
            "run_id": run_id,
            "state": snapshot.state.value,
            "checkpoint_ref": run_id if snapshot.checkpoint is not None else None,
            "usage": snapshot.usage.model_dump(mode="json"),
        }
    )
    return {
        "run": run_value,
        "steps": {
            "items": steps,
            "total": len(owned_steps),
            "next_before_sequence": page_steps[-1].run_sequence if len(selected) > limit else None,
            "latest_sequence": owned_steps[0].run_sequence if owned_steps else None,
            "read_sequence": state.revision,
        },
        "operations": operations,
        "tool_results": references,
    }


@router.get("/executions/{execution_id}/graph-runs")
async def graph_runs(
    execution_id: str,
    cursor: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
    service: ExecutionService = service_dependency,
) -> JsonObject:
    """List independent GraphRun identities and their exact definition revisions."""
    page = await asyncio.to_thread(service.details.graph_runs, execution_id, cursor=cursor, limit=limit)
    roots, snapshots = await sdk_lineage(service, execution_id)
    return {
        **page.model_dump(mode="json", exclude={"items"}),
        "items": [
            {**graph_metadata(value), "root_run_id": root_run_identity(value.parent_run_id, roots, snapshots)}
            for value in page.items
        ],
    }


@router.get("/executions/{execution_id}/graph-runs/{graph_run_id}")
async def graph_run(
    execution_id: str,
    graph_run_id: str,
    cursor: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=100),
    service: ExecutionService = service_dependency,
) -> JsonObject:
    """Read one owned graph and a bounded real attempt page at one product watermark."""
    page = await asyncio.to_thread(
        service.details.graph_run_page, execution_id, graph_run_id, cursor=cursor, limit=limit
    )
    roots, snapshots = await sdk_lineage(service, execution_id)
    return {
        "graph_run": {
            **graph_metadata(page.graph_run),
            "root_run_id": root_run_identity(page.graph_run.parent_run_id, roots, snapshots),
        },
        "graph": page.graph.model_dump(mode="json"),
        "node_attempts": [
            attempt.model_dump(mode="json", exclude={"checkpoint", "result"}) for attempt in page.node_attempts.items
        ],
        "total": page.node_attempts.total,
        "next_cursor": page.node_attempts.next_cursor,
        "read_sequence": page.node_attempts.read_sequence,
    }


@router.get("/executions/{execution_id}/node-attempts/{node_attempt_id}")
async def node_attempt(
    execution_id: str, node_attempt_id: str, service: ExecutionService = service_dependency
) -> JsonObject:
    """Read one verified source attempt with explicit output references."""
    value = await asyncio.to_thread(service.details.node_attempt, execution_id, node_attempt_id)
    return {
        **value.model_dump(mode="json", exclude={"checkpoint", "result"}),
        "output_ref": value.artifact_ids[0] if value.artifact_ids else None,
    }


@router.get("/executions/{execution_id}/tool-results/{result_id:path}")
async def tool_result(
    execution_id: str,
    result_id: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=8192, ge=1, le=32768),
    view: Literal["raw", "display"] = "raw",
    service: ExecutionService = service_dependency,
) -> JsonObject:
    """Read owned raw or readable tool content through bounded UTF-8 pages."""
    if result_id.startswith("operation/"):
        parts = result_id.split("/")
        if len(parts) != 3 or not all(parts):
            raise LookupError("The operation result reference is invalid")
        run_id, operation_id = parts[1:]
        owner = await asyncio.to_thread(run_owner, service, execution_id, run_id)
        journal = await service.sessions.snapshot(owner[0])
        operation = next(
            (value for value in journal.operations if value.run_id == run_id and value.operation_id == operation_id),
            None,
        )
        if operation is None or operation.result is None:
            raise LookupError("The original operation result is unavailable")
        text = operation.result.model_dump_json()
        sequence = journal.revision
        scope: Literal["session", "product"] = "session"
    else:
        observation, sequence = await asyncio.to_thread(service.details.tool_result, execution_id, result_id)
        text = observation.text
        scope = "product"
    content_format: Literal["text", "markdown", "code"] = "text"
    if view == "display":
        text, content_format = display_tool_observation(text)
    value = text_page(result_id, text, offset=offset, max_bytes=limit, read_sequence=sequence, read_scope=scope)
    service.metrics.content_bytes.record(
        len(value.text.encode("utf-8")), {"kind": "tool_result", "direction": "download"}
    )
    response = value.model_dump(mode="json")
    if view == "display":
        response["format"] = content_format
    return response
