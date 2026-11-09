import asyncio
import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from pleiad.contracts import JsonValue
from pleiad.errors.base import ErrorType
from pleiad.messages.message import AssistantMessage, ToolCall, ToolMessage
from pleiad.messages.results import OperationResultReference, ToolResultReference
from pleiad.runtime.limits import RunLimits
from pleiad.sandbox.models import SandboxReceipt, SandboxRequest, SandboxResult
from pleiad.sandbox.sandbox import Sandbox
from pleiad.sessions.input import InputEnvelope
from pleiad.sessions.model_step import ModelStep
from pleiad.sessions.session import Session
from pleiad.sessions.writer import SessionWriter
from pleiad.tools.contracts import ParentRunLimits, ToolContext, ToolResult
from pydantic import SecretStr

from pleiad_weave.agent.contracts import GraphSpec, NodeSpec
from pleiad_weave.agent.graph_controls import GraphControls
from pleiad_weave.api.app import create_app
from pleiad_weave.api.security import MAX_UPLOAD_REQUEST_BYTES
from pleiad_weave.application.client import WeaveClient
from pleiad_weave.infrastructure.storage.model_inputs import source_page
from pleiad_weave.infrastructure.storage.observations import DirectoryObserver
from pleiad_weave.infrastructure.storage.product import JSON_OBJECT
from pleiad_weave.infrastructure.storage.product_details import TextPage, ToolResultDetail
from pleiad_weave.infrastructure.storage.product_models import (
    ChatMessageRequest,
    ExecutionDetailView,
    ExecutionRun,
    ExecutionRunPage,
    ExecutionSpec,
    ExecutionStatus,
    MessageView,
    Report,
    RouteDecision,
    SequencePage,
    SpaceConfiguration,
)
from tests.test_graph_details import NodeModel, node_runner
from tests.test_product_store import create_execution
from tests.test_super_agent_application import (
    ProductScenarioModel,
    VerificationSandbox,
    VerificationScenarioModel,
    product_service,
)


def test_http_same_origin_credential_rejects_unverified_mutations_and_foreign_hosts(tmp_path: Path) -> None:
    async def scenario() -> None:
        model = ProductScenarioModel()
        service = product_service(tmp_path, model)
        app = create_app(service)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8000") as client:
            snapshot = await client.get("/v1/snapshot")
            assert snapshot.status_code == 200
            data = JSON_OBJECT.validate_json(snapshot.content)
            capabilities = data["capabilities"]
            assert isinstance(capabilities, dict) and isinstance(capabilities["csrf_token"], str)
            token = capabilities["csrf_token"]
            body = {"client_message_id": "message", "text": "status"}
            rejected = await client.post("/v1/chat/messages", json=body)
            assert rejected.status_code == 403
            assert JSON_OBJECT.validate_json(rejected.content)["code"] == "http.csrf_rejected"
            origin = await client.post(
                "/v1/chat/messages", json=body, headers={"X-Weave-CSRF": token, "Origin": "https://untrusted.example"}
            )
            assert origin.status_code == 403
            foreign = await client.get("/v1/snapshot", headers={"Host": "untrusted.example"})
            assert foreign.status_code == 403
            malformed = await client.get("/healthz", headers={"Host": "127.0.0.1:invalid"})
            assert malformed.status_code == 400
            assert (await client.get("/v1/conversations/old/messages")).status_code == 404
            assert (await client.get("/healthz")).status_code == 200
            assert (await client.get("/readyz")).status_code == 503
        assert model.requests == [] and service.store.messages().items == ()

    asyncio.run(scenario())


def test_http_chat_retries_publish_one_user_message_and_read_queries_do_not_run_models(tmp_path: Path) -> None:
    async def scenario() -> None:
        model = ProductScenarioModel()
        service = product_service(tmp_path, model)
        app = create_app(service)
        async with (
            app.router.lifespan_context(app),
            AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost:8000") as client,
        ):
            snapshot = JSON_OBJECT.validate_json((await client.get("/v1/snapshot")).content)
            capabilities = snapshot["capabilities"]
            assert isinstance(capabilities, dict) and isinstance(capabilities["csrf_token"], str)
            headers = {"X-Weave-CSRF": capabilities["csrf_token"], "Origin": "http://localhost:8000"}
            body = {"client_message_id": "status", "text": "status"}
            accepted = await client.post("/v1/chat/messages", json=body, headers=headers)
            retry = await client.post("/v1/chat/messages", json=body, headers=headers)
            assert accepted.status_code == retry.status_code == 202
            assert accepted.content == retry.content
            changed = await client.post("/v1/chat/messages", json={**body, "text": "changed"}, headers=headers)
            assert changed.status_code == 409
            async with asyncio.timeout(5):
                await model.status_answered.wait()
            before = len(model.requests)
            for path in (
                "/v1/snapshot",
                "/v1/chat/messages",
                "/v1/spaces",
                "/v1/executions",
                "/v1/attentions",
                "/readyz",
            ):
                assert (await client.get(path)).status_code == 200
            assert len(model.requests) == before
            messages = service.store.messages().items
            assert [value.body for value in messages if value.role == "user"] == ["status"]

    asyncio.run(scenario())


def test_http_multipart_upload_is_idempotent_bounded_and_hides_storage_paths(tmp_path: Path) -> None:
    async def scenario() -> None:
        model = ProductScenarioModel()
        service = product_service(tmp_path, model)
        app = create_app(service)
        async with (
            app.router.lifespan_context(app),
            AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8000") as client,
        ):
            snapshot = JSON_OBJECT.validate_json((await client.get("/v1/snapshot")).content)
            capabilities = snapshot["capabilities"]
            assert isinstance(capabilities, dict) and isinstance(capabilities["csrf_token"], str)
            headers = {"X-Weave-CSRF": capabilities["csrf_token"]}
            files = {"file": ("note.txt", b"original bytes", "text/plain")}
            response = await client.post(
                "/v1/attachments", data={"client_attachment_id": "upload"}, files=files, headers=headers
            )
            assert response.status_code == 201
            value = JSON_OBJECT.validate_json(response.content)
            assert "relative_path" not in value
            identity = value["attachment_id"]
            assert isinstance(identity, str)
            retried = await client.post(
                "/v1/attachments", data={"client_attachment_id": "upload"}, files=files, headers=headers
            )
            assert retried.content == response.content
            changed = await client.post(
                "/v1/attachments",
                data={"client_attachment_id": "upload"},
                files={"file": ("note.txt", b"changed", "text/plain")},
                headers=headers,
            )
            assert changed.status_code == 409
            original = await client.get(f"/v1/attachments/{identity}/content")
            assert original.content == b"original bytes" and "ETag" in original.headers
            oversized = await client.post(
                "/v1/attachments",
                data={"client_attachment_id": "oversized"},
                files={"file": ("large.txt", b"x" * (MAX_UPLOAD_REQUEST_BYTES + 1), "text/plain")},
                headers=headers,
            )
            assert oversized.status_code == 413
            with service.store.transaction() as connection:
                assert connection.execute("SELECT COUNT(*) FROM attachments").fetchone()[0] == 1

        assert model.requests == []

    asyncio.run(scenario())


def test_http_future_event_cursor_requests_resync_without_starting_work(tmp_path: Path) -> None:
    async def scenario() -> None:
        model = ProductScenarioModel()
        service = product_service(tmp_path, model)
        app = create_app(service)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8000") as client:
            response = await client.get("/v1/events?after_sequence=1000")
            assert response.status_code == 200
            assert "event: resync" in response.text
            assert '"snapshot_url": "/v1/snapshot"' in response.text
            assert (await client.get("/v1/events", headers={"Last-Event-ID": "invalid"})).status_code == 400
        assert model.requests == []

    asyncio.run(scenario())


def test_http_settings_expose_configured_status_without_credential_values(tmp_path: Path) -> None:
    async def scenario() -> None:
        model = ProductScenarioModel()
        model.api_key = SecretStr("test-private-credential")
        service = product_service(tmp_path, model)
        app = create_app(service)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8000") as client:
            response = await client.get("/v1/settings")
            assert response.status_code == 200
            assert "test-private-credential" not in response.text and str(tmp_path) not in response.text
            assert JSON_OBJECT.validate_json(response.content)["configuration_locations"] == [
                ".env",
                "capabilities.yaml",
                "mcp.json",
            ]
        assert model.requests == []

    asyncio.run(scenario())


class GatedVerificationSandbox(VerificationSandbox):
    def __init__(self, stdout: bytes) -> None:
        super().__init__(0, stdout=stdout)
        self.accepted = asyncio.Event()
        self.release = asyncio.Event()

    async def execute(
        self, request: SandboxRequest, *, on_accept: Callable[[SandboxReceipt], Awaitable[None]] | None = None
    ) -> SandboxResult:
        result = await super().execute(request, on_accept=on_accept)
        self.accepted.set()
        await self.release.wait()
        return result


@pytest.mark.parametrize("view", ["raw", "display"])
def test_http_background_results_remain_complete_behind_bounded_owned_utf8_pages(tmp_path: Path, view: str) -> None:
    async def scenario() -> None:
        payload = "处理结果" * 20_000
        model = VerificationScenarioModel("run-check")
        service = product_service(tmp_path, model)
        sandbox = GatedVerificationSandbox(payload.encode())
        service.agents.sandbox = Sandbox(sandbox)
        space = service.store.create_space(SpaceConfiguration(name="Checks"), request_id="space")
        foreign = service.store.create_execution(
            ExecutionSpec(space_id=space.space_id, objective="Unrelated work"), request_id="foreign"
        )
        service.store.control_execution(
            foreign.execution_id, "pause", request_id="pause", expected_revision=foreign.revision
        )
        app = create_app(service)
        async with (
            app.router.lifespan_context(app),
            AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8000") as client,
        ):
            execution = await service.delegate(
                ExecutionSpec(space_id=space.space_id, objective="Read the full process result"), "work"
            )
            try:
                async with asyncio.timeout(5):
                    await sandbox.accepted.wait()
                running = await service.require_execution(execution.execution_id)
                assert running.current_run_id is not None
                path = f"/v1/executions/{execution.execution_id}/runs/{running.current_run_id}"
                running_response = await client.get(path)
                assert running_response.status_code == 200
                running_detail = JSON_OBJECT.validate_json(running_response.content)
                operations = running_detail["operations"]
                assert isinstance(operations, list) and len(operations) == 1
                active = operations[0]
                assert isinstance(active, dict)
                assert set(active) == {"operation_id", "call_id", "step_id", "state"}
                assert active["state"] == "running" and active["call_id"] == "unit-tests"
                running_steps = running_detail["steps"]
                assert isinstance(running_steps, dict) and isinstance(running_steps["items"], list)
                assert any(
                    isinstance(step, dict) and step["step_id"] == active["step_id"] for step in running_steps["items"]
                )
                receipts = running_detail["tool_results"]
                assert isinstance(receipts, list)
                assert not any(isinstance(value, dict) and value["kind"] == "operation" for value in receipts)
                unavailable = await client.get(
                    f"/v1/executions/{execution.execution_id}/tool-results/"
                    f"operation/{running.current_run_id}/{active['operation_id']}"
                )
                assert unavailable.status_code == 404
                denied_run = await client.get(f"/v1/executions/{foreign.execution_id}/runs/{running.current_run_id}")
                assert denied_run.status_code == 404
            finally:
                sandbox.release.set()
            async with asyncio.timeout(5):
                finished = await service.wait(execution.execution_id)
                while (await asyncio.to_thread(service.store.snapshot)).routing.state != "idle":
                    await asyncio.to_thread(service.store.events)
            assert finished.status is ExecutionStatus.SUCCEEDED and finished.current_run_id is not None
            detail = await client.get(f"/v1/executions/{execution.execution_id}/runs/{finished.current_run_id}")
            assert detail.status_code == 200 and len(detail.content) < 65_536
            settled_detail = JSON_OBJECT.validate_json(detail.content)
            assert settled_detail["operations"] == [{**active, "state": "succeeded"}]
            sources = settled_detail["tool_results"]
            assert isinstance(sources, list)
            operation = next(value for value in sources if isinstance(value, dict) and value["kind"] == "operation")
            assert operation["step_id"] == active["step_id"] and operation["call_id"] == active["call_id"]
            assert isinstance(operation["tool_result_id"], str)
            reference = operation["tool_result_id"]
            first_response = await client.get(path, params={"limit": 1})
            first = JSON_OBJECT.validate_json(first_response.content)
            assert first["operations"] == [] and first["tool_results"] == []
            history = first
            collected_operations: list[JsonValue] = []
            collected_results: list[JsonValue] = []
            while True:
                step_page = SequencePage[ModelStep].model_validate(history["steps"])
                page_operations, page_results = history["operations"], history["tool_results"]
                assert isinstance(page_operations, list) and isinstance(page_results, list)
                assert len(step_page.items) == 1
                assert all(
                    isinstance(value, dict) and value["step_id"] == step_page.items[0].step_id
                    for value in (*page_operations, *page_results)
                )
                collected_operations.extend(page_operations)
                collected_results.extend(page_results)
                if step_page.next_before_sequence is None:
                    break
                response = await client.get(
                    path, params={"before_sequence": step_page.next_before_sequence, "limit": 1}
                )
                history = JSON_OBJECT.validate_json(response.content)
            assert collected_operations == settled_detail["operations"]
            assert sorted(json.dumps(value, sort_keys=True) for value in collected_results) == sorted(
                json.dumps(value, sort_keys=True) for value in sources
            )
            before = len(model.requests)
            offset = 0
            parts: list[str] = []
            while True:
                response = await client.get(
                    f"/v1/executions/{execution.execution_id}/tool-results/{reference}",
                    params={"view": view, "offset": offset, "limit": 32768},
                )
                assert response.status_code == 200
                page = JSON_OBJECT.validate_json(response.content)
                assert page["read_scope"] == "session" and isinstance(page["text"], str)
                assert len(page["text"].encode()) <= 32768
                parts.append(page["text"])
                cursor = page["next_offset"]
                if cursor is None:
                    break
                assert isinstance(cursor, int) and cursor > offset
                offset = cursor
            if view == "display":
                assert page["format"] == "code"
                assert "".join(parts) == f"命令已结束。退出码 0。\n\n输出\n{payload}"
            else:
                original = JSON_OBJECT.validate_json("".join(parts))
                output = original["output"]
                assert isinstance(output, dict) and output["stdout"] == payload
                assert output["exit_code"] == 0
            denied = await client.get(
                f"/v1/executions/{foreign.execution_id}/tool-results/{reference}", params={"view": view}
            )
            assert denied.status_code == 404
            assert len(model.requests) == before

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("original", "expected", "content_format"),
    [
        (
            json.dumps(
                {
                    "ok": True,
                    "output": source_page(
                        "objective",
                        "private",
                        "# 调研要求\n\n先查来源。",
                        start_char=0,
                        max_chars=4000,
                        expected_digest=None,
                        tool="query_work",
                    ),
                }
            ),
            "# 调研要求\n\n先查来源。",
            "markdown",
        ),
        (
            json.dumps({"ok": False, "error_message": "无法访问目标文件。", "error_code": "file.not_found"}),
            "操作未完成\n\n无法访问目标文件。",
            "text",
        ),
        (
            json.dumps({"ok": True, "output": {"status": "started", "operation_id": "private"}}),
            "操作已启动",
            "text",
        ),
        (
            json.dumps(
                {
                    "ok": True,
                    "output": {
                        "title": "订单",
                        "count": 2,
                        "id": "order-123",
                        "customer_id": "customer-456",
                        "status": "pending",
                        "kind": "purchase",
                    },
                }
            ),
            "标题\n订单\n\n数量\n2\n\nid\norder-123\n\ncustomer id\ncustomer-456\n\n状态\npending\n\nkind\npurchase",
            "text",
        ),
        (
            json.dumps(
                {
                    "ok": True,
                    "output": source_page(
                        "objective",
                        "private",
                        "部分文件内容及其余要求",
                        start_char=0,
                        max_chars=6,
                        expected_digest=None,
                        tool="query_work",
                    ),
                }
            ),
            "部分文件内容\n\n内容已截断。",
            "markdown",
        ),
        (
            json.dumps(
                {
                    "ok": True,
                    "output": {
                        "path": "script.py",
                        "line_start": 1,
                        "line_end": 2,
                        "sha256": "private",
                        "truncated": False,
                        "content": "# 实际代码注释\nprint('结果')",
                    },
                }
            ),
            "文件\nscript.py\n\n起始行\n1\n\n结束行\n2\n\n# 实际代码注释\nprint('结果')",
            "text",
        ),
        (
            json.dumps(
                {
                    "ok": True,
                    "output": {
                        "stdout": "已处理一页",
                        "stderr": "连接断开",
                        "exit_code": 1,
                        "timed_out": True,
                        "output_truncated": True,
                        "decoding_loss": True,
                    },
                }
            ),
            "命令已结束。退出码 1。\n\n输出\n已处理一页\n\n错误输出\n连接断开\n\n命令执行超时。"
            "\n\n输出已截断。\n\n输出包含无法识别的字符。",
            "code",
        ),
        (
            json.dumps(
                {
                    "ok": True,
                    "output": {
                        "content": "检索正文",
                        "url": "https://example.com",
                        "warning": "内容尚未核实",
                        "order_id": "order-789",
                    },
                }
            ),
            "链接\nhttps://example.com\n\n提醒\n内容尚未核实\n\norder id\norder-789\n\n检索正文",
            "markdown",
        ),
        (
            json.dumps({"ok": True, "output": {"path": "notes.md", "bytes_written": 24, "sha256": "private"}}),
            "文件\nnotes.md\n\n已写入字节数\n24",
            "text",
        ),
        (
            json.dumps({"ok": False, "error_message": "导入未完成", "output": {"count": 1, "order_ids": ["order-1"]}}),
            "操作未完成\n\n导入未完成\n\n数量\n1\n\norder ids\n• order-1",
            "text",
        ),
        ("工具返回的普通文本。\n保留实际换行。", "工具返回的普通文本。\n保留实际换行。", "text"),
        (
            json.dumps(
                {
                    "ok": True,
                    "output": {
                        "content": "订单正文",
                        "reference": {"operation_id": "order-op", "customer_id": "customer-1"},
                        "start_char": 0,
                        "next_start_char": 4,
                        "truncated": False,
                    },
                }
            ),
            "reference\noperation id\norder-op\n\ncustomer id\ncustomer-1"
            "\n\nstart char\n0\n\nnext start char\n4\n\ntruncated\n否\n\n订单正文",
            "markdown",
        ),
        *[
            (
                json.dumps(
                    {
                        "ok": True,
                        "output": source_page(
                            kind,
                            "private",
                            json.dumps(record),
                            start_char=0,
                            max_chars=4000,
                            expected_digest=None,
                            tool="query_work",
                        ),
                    }
                ),
                expected,
                "markdown",
            )
            for kind, record, expected in [
                (
                    "space",
                    {"name": "调研空间", "instructions": "核对来源", "space_id": "private", "revision": 2},
                    "名称\n调研空间\n\n工作要求\n核对来源",
                ),
                (
                    "execution",
                    {
                        "objective": "研究模型",
                        "status": "paused",
                        "execution_id": "private",
                        "session_id": "private",
                        "input_watermark": 17,
                    },
                    "任务要求\n研究模型\n\n状态\n已暂停",
                ),
                (
                    "message",
                    {"body": "需要附来源链接", "message_id": "private", "chat_sequence": 3},
                    "消息正文\n需要附来源链接",
                ),
                (
                    "disposition",
                    {"state": "handled", "input_id": "private", "origin_input_id": "private", "revision": 3},
                    "处理状态\n已处理",
                ),
            ]
        ],
        (
            json.dumps(
                {
                    "ok": True,
                    "output": source_page(
                        "execution",
                        "private",
                        json.dumps({"objective": "调研要求", "execution_id": "private"}),
                        start_char=0,
                        max_chars=12,
                        expected_digest=None,
                        tool="query_work",
                    ),
                }
            ),
            "资料记录不完整。\n\n内容已截断。",
            "markdown",
        ),
    ],
)
def test_http_readable_tool_results_present_content_without_runtime_metadata(
    tmp_path: Path, original: str, expected: str, content_format: str
) -> None:
    async def scenario() -> None:
        model = ProductScenarioModel()
        service = product_service(tmp_path, model)
        owner = create_execution(service.store, "owner")
        foreign = service.store.create_execution(
            ExecutionSpec(space_id=owner.space_id, objective="Unrelated work"), request_id="foreign"
        )
        result_id = "run/observation"
        service.details.save_tool_result(
            ToolResultDetail(
                tool_result_id=result_id,
                execution_id=owner.execution_id,
                run_id="run",
                step_id="step",
                call_id="call",
                text=original,
            )
        )
        app = create_app(service)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8000") as client:
            path = f"/v1/executions/{owner.execution_id}/tool-results/{result_id}"
            raw = await client.get(path)
            assert JSON_OBJECT.validate_json(raw.content)["text"] == original
            parts: list[str] = []
            offset = 0
            while True:
                response = await client.get(path, params={"view": "display", "offset": offset, "limit": 13})
                assert response.status_code == 200
                value = JSON_OBJECT.validate_json(response.content)
                assert value["format"] == content_format
                assert isinstance(value["text"], str) and len(value["text"].encode()) <= 13
                parts.append(value["text"])
                cursor = value["next_offset"]
                if cursor is None:
                    break
                assert isinstance(cursor, int) and cursor > offset
                offset = cursor
            assert "".join(parts) == expected
            denied = await client.get(
                f"/v1/executions/{foreign.execution_id}/tool-results/{result_id}", params={"view": "display"}
            )
            assert denied.status_code == 404
        assert model.requests == []

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("kind", "window", "scenario_name", "expected", "content_format"),
    [
        (
            "operation",
            "complete",
            "command",
            "命令已结束。退出码 1。\n\n输出\n已处理一页\n\n错误输出\n连接断开\n\n输出已截断。",
            "code",
        ),
        (
            "message",
            "complete",
            "command",
            "命令已结束。退出码 1。\n\n输出\n已处理一页\n\n错误输出\n连接断开\n\n输出已截断。",
            "code",
        ),
        ("operation", "first", "command", "结果记录不完整。\n\n内容已截断。", "text"),
        ("operation", "last", "command", "结果记录不完整。", "text"),
        ("operation", "complete", "failure", "操作未完成\n\n无法读取文件。", "text"),
        (
            "operation",
            "complete",
            "business",
            "order id\norder-123\n\ncustomer id\ncustomer-456\n\n状态\npending",
            "text",
        ),
        ("message", "complete", "plain", "工具返回的普通文本。\n保留实际换行。", "text"),
    ],
)
def test_http_readable_tool_result_pages_unwrap_sdk_results_and_preserve_incomplete_reads(
    tmp_path: Path, kind: str, window: str, scenario_name: str, expected: str, content_format: str
) -> None:
    async def scenario() -> None:
        now = datetime.now(UTC)
        inner = ToolResult(
            call_id="private-inner-call",
            ok=scenario_name != "failure",
            output={"stdout": "已处理一页", "stderr": "连接断开", "exit_code": 1, "output_truncated": True},
            error_code="file.not_found" if scenario_name == "failure" else None,
            error_message="无法读取文件。" if scenario_name == "failure" else None,
            error_type=ErrorType.BUSINESS if scenario_name == "failure" else None,
            started_at=now,
            finished_at=now,
        )
        if scenario_name == "failure":
            inner = inner.model_copy(update={"output": None})
        if scenario_name == "business":
            inner = inner.model_copy(
                update={"output": {"order_id": "order-123", "customer_id": "customer-456", "status": "pending"}}
            )
        content = "工具返回的普通文本。\n保留实际换行。" if scenario_name == "plain" else inner.model_dump_json()
        reference = (
            OperationResultReference.from_result("private-operation", inner)
            if kind == "operation"
            else ToolResultReference.from_message(ToolMessage(tool_call_id="private-call", content=content))
        )
        start = 12 if window == "last" else 0
        end = 12 if window == "first" else len(content)
        outer = ToolResult(
            call_id="private-reader-call",
            ok=True,
            output={
                "reference": reference.model_dump(mode="json"),
                "start_char": start,
                "next_start_char": end,
                "content": content[start:end],
                "truncated": end < len(content),
            },
            started_at=now,
            finished_at=now,
        )
        service = product_service(tmp_path, ProductScenarioModel())
        owner = create_execution(service.store, "owner")
        service.details.save_tool_result(
            ToolResultDetail(
                tool_result_id="reader-result",
                execution_id=owner.execution_id,
                run_id="run",
                step_id="step",
                call_id="private-reader-call",
                text=outer.model_dump_json(),
            )
        )
        async with AsyncClient(
            transport=ASGITransport(app=create_app(service)), base_url="http://127.0.0.1:8000"
        ) as client:
            path = f"/v1/executions/{owner.execution_id}/tool-results/reader-result"
            response = await client.get(path, params={"view": "display", "limit": 32768})
            assert response.status_code == 200
            page = JSON_OBJECT.validate_json(response.content)
            assert page["text"] == expected and page["format"] == content_format
            assert page["next_offset"] is None
            raw = await client.get(path, params={"view": "raw", "limit": 32768})
            assert JSON_OBJECT.validate_json(raw.content)["text"] == outer.model_dump_json()

    asyncio.run(scenario())


def test_http_readable_work_overview_preserves_preview_and_paging_facts_without_record_ids(tmp_path: Path) -> None:
    async def scenario() -> None:
        service = product_service(tmp_path, ProductScenarioModel())
        owner = create_execution(service.store, "owner")
        overview = {
            "execution": {
                "execution_id": owner.execution_id,
                "objective_preview": "研究模型",
                "status": "paused",
                "reference": {"reference": "weave:execution:private", "digest": "private", "summary": "private"},
            },
            "messages": [{"message_id": "private", "body_preview": "附来源链接"}],
            "inputs": [{"input_id": "private", "content": "先查询来源"}],
            "responsibilities": [{"origin_input_id": "private", "state": "owned", "required": True}],
            "tool_results": [{"tool_result_id": "private", "call_id": "private", "run_id": "private"}],
            "total_inputs": 1,
            "total_messages": 1,
            "total_responsibilities": 1,
            "next_cursor": 1,
        }
        service.details.save_tool_result(
            ToolResultDetail(
                tool_result_id="overview",
                execution_id=owner.execution_id,
                run_id="run",
                step_id="step",
                call_id="call",
                text=json.dumps({"ok": True, "output": overview}),
            )
        )
        async with AsyncClient(
            transport=ASGITransport(app=create_app(service)), base_url="http://127.0.0.1:8000"
        ) as client:
            response = await client.get(
                f"/v1/executions/{owner.execution_id}/tool-results/overview", params={"view": "display"}
            )
            assert response.status_code == 200
            content = JSON_OBJECT.validate_json(response.content)["text"]
            assert content == (
                "任务\n任务要求节选\n研究模型\n\n状态\n已暂停\n\n相关消息\n• 消息正文节选\n附来源链接"
                "\n\n处理情况\n• 处理状态\n由当前任务处理\n\n需要完成\n是\n\n收到的内容\n• 先查询来源"
                "\n\n收到的内容总数\n1\n\n相关消息总数\n1\n\n处理记录总数\n1\n\n本次读取的工具结果数\n1"
                "\n\n还有更多记录。"
            )

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "original",
    [
        '{"ok":"true","output":{}}',
        '{"ok":true,"output":null}',
        '{"ok":false,"error_message":null}',
        '{"ok":true,"output":{"stdout":"partial","exit_code":0}}',
        *[
            json.dumps(
                {
                    "ok": True,
                    "output": {
                        "reference": {
                            "message_id": "message",
                            "sha256": "a" * 64,
                            "total_chars": 4,
                        },
                        "start_char": 0,
                        "next_start_char": end,
                        "content": "正文内容",
                        "truncated": truncated,
                    },
                }
            )
            for end, truncated in [(4, False), (3, False), (4, True)]
        ],
    ],
)
def test_http_readable_tool_results_reject_incomplete_structured_observations(tmp_path: Path, original: str) -> None:
    async def scenario() -> None:
        service = product_service(tmp_path, ProductScenarioModel())
        owner = create_execution(service.store, "owner")
        service.details.save_tool_result(
            ToolResultDetail(
                tool_result_id="invalid",
                execution_id=owner.execution_id,
                run_id="run",
                step_id="step",
                call_id="call",
                text=original,
            )
        )
        async with AsyncClient(
            transport=ASGITransport(app=create_app(service)), base_url="http://127.0.0.1:8000"
        ) as client:
            response = await client.get(
                f"/v1/executions/{owner.execution_id}/tool-results/invalid", params={"view": "display"}
            )
        assert response.status_code == 422
        assert JSON_OBJECT.validate_json(response.content)["code"] == "product.invalid_request"

    asyncio.run(scenario())


def test_http_step_history_starts_with_latest_step_and_keeps_older_pages_stable(tmp_path: Path) -> None:
    async def scenario() -> None:
        model = ProductScenarioModel()
        service = product_service(tmp_path, model)
        owner = create_execution(service.store, "history")
        session = Session(owner.session_id, service.sessions)
        await session.enqueue(InputEnvelope(input_id="input", kind="user", source="test", content="input"))
        started = await service.sessions.start(
            session.session_id,
            invocation_id="run",
            parameters_digest=hashlib.sha256(b"run").hexdigest(),
            input_ids=("input",),
            input_mode="batch",
            agent_id="agent",
        )
        writer = SessionWriter(session, started.snapshot)
        service.store.record_execution_run(
            ExecutionRun(execution_id=owner.execution_id, attempt=1, invocation_id="run", run_id=writer.run_id)
        )
        for number in range(1, 55):
            step = await writer.begin("agent")
            await writer.complete(step.step_id, (AssistantMessage(content=f"Step {number}"),))
        async with AsyncClient(
            transport=ASGITransport(app=create_app(service)), base_url="http://127.0.0.1:8000"
        ) as client:
            path = f"/v1/executions/{owner.execution_id}/runs/{writer.run_id}"
            first_response = await client.get(path)
            assert first_response.status_code == 200
            first = SequencePage[ModelStep].model_validate(JSON_OBJECT.validate_json(first_response.content)["steps"])
            assert [step.run_sequence for step in first.items] == list(range(54, 4, -1))
            assert first.next_before_sequence == 5 and first.latest_sequence == first.total == 54
            step = await writer.begin("agent")
            await writer.complete(step.step_id, (AssistantMessage(content="New progress"),))
            older_response = await client.get(path, params={"before_sequence": first.next_before_sequence})
            older = SequencePage[ModelStep].model_validate(JSON_OBJECT.validate_json(older_response.content)["steps"])
            assert [step.run_sequence for step in older.items] == [4, 3, 2, 1]
            assert older.next_before_sequence is None and older.latest_sequence == older.total == 55
            frozen_response = await client.get(path, params={"before_sequence": 55})
            frozen = SequencePage[ModelStep].model_validate(JSON_OBJECT.validate_json(frozen_response.content)["steps"])
            assert frozen.items == first.items and frozen.latest_sequence == 55
            latest_response = await client.get(path, params={"limit": 1})
            latest = SequencePage[ModelStep].model_validate(JSON_OBJECT.validate_json(latest_response.content)["steps"])
            assert latest.items[0].run_sequence == 55
            assert (await client.get(path, params={"before_sequence": 0})).status_code == 422
        state = await service.sessions.snapshot(owner.session_id)
        assert [step.run_sequence for step in state.steps] == list(range(1, 56))
        assert model.requests == []

    asyncio.run(scenario())


def test_http_run_history_exposes_latest_attempt_beyond_a_page_and_preserves_pending_state(tmp_path: Path) -> None:
    async def scenario() -> None:
        model = ProductScenarioModel()
        service = product_service(tmp_path, model)
        owner = create_execution(service.store, "history")
        for attempt in range(1, 53):
            service.store.record_execution_run(
                ExecutionRun(
                    execution_id=owner.execution_id,
                    attempt=attempt,
                    invocation_id=f"invocation-{attempt}",
                    run_id=f"run-{attempt}",
                    state="succeeded",
                )
            )
        async with AsyncClient(
            transport=ASGITransport(app=create_app(service)), base_url="http://127.0.0.1:8000"
        ) as client:
            path = f"/v1/executions/{owner.execution_id}/runs"
            first_response = await client.get(path)
            assert first_response.status_code == 200
            first = ExecutionRunPage.model_validate_json(first_response.content)
            assert [run.attempt for run in first.items] == list(range(52, 2, -1))
            assert first.latest_run_id == "run-52" and first.next_before_sequence == 3
            service.store.record_execution_run(
                ExecutionRun(execution_id=owner.execution_id, attempt=53, invocation_id="invocation-53")
            )
            older_response = await client.get(path, params={"before_sequence": first.next_before_sequence})
            older = ExecutionRunPage.model_validate_json(older_response.content)
            assert [run.attempt for run in older.items] == [2, 1]
            assert older.latest_sequence == older.total == 53 and older.latest_run_id is None
            assert older.next_before_sequence is None
            newest = ExecutionRunPage.model_validate_json((await client.get(path)).content)
            assert newest.items[0].attempt == 53 and newest.items[0].run_id is None
        assert model.requests == []

    asyncio.run(scenario())


def test_http_execution_details_reject_foreign_run_graph_and_raw_result_identities(tmp_path: Path) -> None:
    async def scenario() -> None:
        model = ProductScenarioModel()
        service = product_service(tmp_path, model)
        space = service.store.create_space(SpaceConfiguration(name="Worker"), request_id="space")
        owner = service.store.create_execution(
            ExecutionSpec(space_id=space.space_id, objective="Owner"), request_id="owner"
        )
        other = service.store.create_execution(
            ExecutionSpec(space_id=space.space_id, objective="Other"), request_id="other"
        )
        session = Session(owner.session_id, service.sessions)
        await session.enqueue(InputEnvelope(input_id="input", kind="user", source="test", content="input"))
        started = await service.sessions.start(
            session.session_id,
            invocation_id="run",
            parameters_digest=hashlib.sha256(b"run").hexdigest(),
            input_ids=("input",),
            input_mode="batch",
            agent_id="agent",
        )
        writer = SessionWriter(session, started.snapshot)
        step = await writer.begin("agent")
        raw = "original private tool observation " * 1000
        observation = ToolMessage(tool_call_id="call", content=raw)
        await writer.complete(
            step.step_id,
            (AssistantMessage(tool_calls=(ToolCall(id="call", name="lookup", arguments="{}"),)), observation),
        )
        service.store.record_execution_run(
            ExecutionRun(
                execution_id=owner.execution_id, attempt=1, invocation_id="run", run_id=writer.run_id, state="running"
            )
        )
        result_id = f"{writer.run_id}/{observation.message_id}"
        service.details.save_tool_result(
            ToolResultDetail(
                tool_result_id=result_id,
                execution_id=owner.execution_id,
                run_id=writer.run_id,
                step_id=step.step_id,
                call_id="call",
                text=raw,
            )
        )
        definition = service.details.submit(
            owner.execution_id,
            GraphSpec(graph_id="graph", nodes=(NodeSpec(node_id="node", objective="Goal"),)),
            expected_revision=None,
        )
        graph, duplicate = service.details.start_graph(
            owner.execution_id, definition, parent_run_id=writer.run_id, call_id="graph", input_value={}
        )
        assert not duplicate
        app = create_app(service)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8000") as client:
            prefix = f"/v1/executions/{owner.execution_id}"
            metadata = await client.get(f"{prefix}/runs/{writer.run_id}")
            assert metadata.status_code == 200 and raw not in metadata.text
            original = await client.get(f"{prefix}/tool-results/{result_id}?limit=8192")
            assert original.status_code == 200
            data = JSON_OBJECT.validate_json(original.content)
            assert data["text"] == raw[:8192] and data["next_offset"] == 8192
            assert (await client.get(f"{prefix}/graph-runs/{graph.graph_run_id}")).status_code == 200
            foreign = f"/v1/executions/{other.execution_id}"
            for path in (
                f"{foreign}/runs/{writer.run_id}",
                f"{foreign}/graph-runs/{graph.graph_run_id}",
                f"{foreign}/tool-results/{result_id}",
            ):
                assert (await client.get(path)).status_code == 404
            executions = JSON_OBJECT.validate_json((await client.get("/v1/executions")).content)["items"]
            assert isinstance(executions, list) and executions
            assert isinstance(executions[0], dict) and "available_actions" in executions[0]
        assert model.requests == []

    asyncio.run(scenario())


def test_http_directory_observation_requires_explicit_owner_action_and_never_dispatches_work(tmp_path: Path) -> None:
    async def scenario() -> None:
        external = tmp_path / "external-source"
        external.mkdir()
        note = external / "note.txt"
        note.write_bytes(b"observed source")
        model = ProductScenarioModel()
        service = product_service(tmp_path, model)
        observer = DirectoryObserver(service.store, external)
        service.observer = observer
        app = create_app(service)
        async with (
            app.router.lifespan_context(app),
            AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8000") as client,
        ):
            sources = await client.get("/v1/sources")
            assert sources.status_code == 200 and str(external) not in sources.text
            assert (await client.get(f"/v1/sources/{observer.source_id}/health")).status_code == 200
            assert observer.observations().items == ()
            denied = await client.post(f"/v1/sources/{observer.source_id}/observe", json={"request_id": "scan"})
            assert denied.status_code == 403 and observer.observations().items == ()
            capabilities = JSON_OBJECT.validate_json((await client.get("/v1/snapshot")).content)["capabilities"]
            assert isinstance(capabilities, dict) and isinstance(capabilities["csrf_token"], str)
            headers = {"X-Weave-CSRF": capabilities["csrf_token"]}
            first = await client.post(
                f"/v1/sources/{observer.source_id}/observe", json={"request_id": "scan"}, headers=headers
            )
            assert first.status_code == 200
            retry = await client.post(
                f"/v1/sources/{observer.source_id}/observe", json={"request_id": "scan"}, headers=headers
            )
            assert JSON_OBJECT.validate_json(retry.content)["duplicate"] is True
            note.write_bytes(b"changed source")
            readonly = await client.get(f"/v1/sources/{observer.source_id}/observations")
            assert JSON_OBJECT.validate_json(readonly.content)["total"] == 1
            changed = await client.post(
                f"/v1/sources/{observer.source_id}/observe", json={"request_id": "changed"}, headers=headers
            )
            assert changed.status_code == 200
            assert [value.kind for value in observer.observations().items] == ["created", "changed"]
            assert service.store.list_executions().items == ()
        assert model.requests == []

    asyncio.run(scenario())


def test_web_entry_serves_bundled_assets_with_same_origin_security_headers(tmp_path: Path) -> None:
    async def scenario() -> None:
        service = product_service(tmp_path, ProductScenarioModel())
        async with AsyncClient(transport=ASGITransport(app=create_app(service)), base_url="http://127.0.0.1") as client:
            index = await client.get("/")
            assert index.status_code == 200 and index.headers["content-type"].startswith("text/html")
            assert index.headers["cache-control"] == "no-cache"
            assert index.headers["x-content-type-options"] == "nosniff"
            assert index.headers["referrer-policy"] == "same-origin"
            policy = index.headers["content-security-policy"]
            assert "connect-src 'self'" in policy and "frame-ancestors 'none'" in policy
            assets = re.findall(r'(?:src|href)="(/assets/[^\"]+)"', index.text)
            assert assets
            for asset in assets:
                response = await client.get(asset)
                assert response.status_code == 200 and response.content
                assert not response.headers["content-type"].startswith("text/html")
            unknown = await client.get("/v1/not-an-api")
            assert unknown.status_code == 404 and unknown.headers["content-type"].startswith("application/json")

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["attachment", "artifact"])
def test_http_text_preview_reassembles_complete_content_through_bounded_utf8_pages(tmp_path: Path, kind: str) -> None:
    """Text previews preserve exact original bytes behind the shared page contract."""

    async def scenario() -> None:
        model = ProductScenarioModel()
        service = product_service(tmp_path, model)
        payload = ("原始文本🙂,value\n" * 10_000).encode("utf-8")
        if kind == "attachment":
            attachment = service.content.upload_attachment(
                client_attachment_id="text", name="original.csv", mime="text/csv", payload=payload
            )
            identity = attachment.attachment_id
        else:
            execution = create_execution(service.store, "text-preview")
            artifact = service.content.install_artifact(
                execution.execution_id, artifact_id="text", name="original.csv", mime="text/csv", payload=payload
            )
            assert artifact.previewable
            identity = artifact.artifact_id
        path = f"/v1/{kind}s/{identity}/text"
        chunks: list[bytes] = []
        offset = 0
        async with AsyncClient(transport=ASGITransport(app=create_app(service)), base_url="http://127.0.0.1") as client:
            while True:
                response = await client.get(path, params={"offset": offset})
                assert response.status_code == 200
                page = TextPage.model_validate_json(response.content)
                chunk = page.text.encode("utf-8")
                assert len(chunk) <= 8192 and page.offset == offset
                assert page.id == identity and page.total_bytes == len(payload)
                assert page.read_scope == "product" and page.read_sequence > 0
                chunks.append(chunk)
                if page.next_offset is None:
                    break
                offset = page.next_offset
            assert b"".join(chunks) == payload
            maximum = await client.get(path, params={"max_bytes": 32768})
            assert maximum.status_code == 200
            assert len(TextPage.model_validate_json(maximum.content).text.encode("utf-8")) <= 32768
            for parameters in ({"offset": 1}, {"offset": len(payload) + 1}, {"max_bytes": 32769}):
                assert (await client.get(path, params=parameters)).status_code == 422
            assert (await client.get(f"/v1/{kind}s/missing/text")).status_code == 404
        assert model.requests == []

    asyncio.run(scenario())


def test_http_text_preview_rejects_binary_artifacts_and_corrupt_original_bytes(tmp_path: Path) -> None:
    """A preview cannot disguise unsupported or changed original content as valid text."""

    async def scenario() -> None:
        service = product_service(tmp_path, ProductScenarioModel())
        execution = create_execution(service.store, "preview-integrity")
        binary = service.content.install_artifact(
            execution.execution_id,
            artifact_id="binary",
            name="data.bin",
            mime="application/octet-stream",
            payload=b"binary",
        )
        text = service.content.install_artifact(
            execution.execution_id, artifact_id="text", name="result.txt", mime="text/plain", payload=b"actual text"
        )
        (service.store.content_root / text.relative_path).write_bytes(b"changed text")
        async with AsyncClient(transport=ASGITransport(app=create_app(service)), base_url="http://127.0.0.1") as client:
            assert (await client.get(f"/v1/artifacts/{binary.artifact_id}/text")).status_code == 422
            assert (await client.get(f"/v1/artifacts/{text.artifact_id}/text")).status_code == 409
        current = service.store.get_artifact(text.artifact_id)
        assert current is not None and current.deletion_state == "corrupt"

    asyncio.run(scenario())


def test_http_text_preview_rejects_invalid_utf8_and_revoked_attachment_content(tmp_path: Path) -> None:
    """Preview decoding and content lifecycle retain explicit errors at their real boundary."""

    async def scenario() -> None:
        service = product_service(tmp_path, ProductScenarioModel())
        execution = create_execution(service.store, "preview-text-contract")
        invalid = service.content.install_artifact(
            execution.execution_id, artifact_id="invalid-text", name="result.txt", mime="text/plain", payload=b"\xff"
        )
        attachment = service.content.upload_attachment(
            client_attachment_id="revoked", name="staged.txt", mime="text/plain", payload=b"staged text"
        )
        service.content.delete_attachment(attachment.attachment_id, request_id="revoke")
        async with AsyncClient(transport=ASGITransport(app=create_app(service)), base_url="http://127.0.0.1") as client:
            assert (await client.get(f"/v1/artifacts/{invalid.artifact_id}/text")).status_code == 422
            assert (await client.get(f"/v1/attachments/{attachment.attachment_id}/text")).status_code == 409

    asyncio.run(scenario())


def test_event_replay_overflow_requests_a_new_snapshot_before_publishing_any_partial_events(tmp_path: Path) -> None:
    async def scenario() -> None:
        model = ProductScenarioModel()
        service = product_service(tmp_path, model)
        with service.store.transaction() as connection:
            for number in range(1001):
                service.store.emit(connection, str(number), "source.observed", {"observation": number})
        async with AsyncClient(transport=ASGITransport(app=create_app(service)), base_url="http://127.0.0.1") as client:
            response = await client.get("/v1/events?after_sequence=0")
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            assert response.text.startswith("event: resync\n") and "event: product" not in response.text
            payload = json.loads(response.text.split("data: ", 1)[1])
            assert payload == {"snapshot_url": "/v1/snapshot", "snapshot_sequence": 1001}
            snapshot = JSON_OBJECT.validate_json((await client.get("/v1/snapshot")).content)
            assert snapshot["snapshot_sequence"] == 1001
            assert model.requests == []

    asyncio.run(scenario())


def test_chat_reports_project_owned_artifacts_without_exposing_storage_paths(tmp_path: Path) -> None:
    async def scenario() -> None:
        service = product_service(tmp_path, ProductScenarioModel())
        execution = create_execution(service.store, "report", running=True)
        artifact = service.content.install_artifact(
            execution.execution_id,
            artifact_id="report-artifact",
            name="Evidence.md",
            mime="text/markdown",
            payload=b"# Verified evidence\n",
        )
        report = service.store.accept_report(
            Report(
                report_id="ready-report",
                execution_id=execution.execution_id,
                run_id="run-report",
                input_watermark=0,
                kind="ready",
                summary="Verified the evidence",
                artifact_ids=(artifact.artifact_id,),
            )
        )
        assert report.rejection_reasons == ()
        batch = service.store.claim_router_batch()
        assert batch is not None
        service.store.save_route_candidate(
            RouteDecision(
                decision_id=batch.decision_id,
                source_input_ids=batch.source_input_ids,
                reply="The evidence is ready to inspect",
            ),
            attempt=batch.attempt,
            sdk_run_id="router-report",
        )
        service.store.apply_route_decision(batch.decision_id, attempt=batch.attempt)
        transport = ASGITransport(app=create_app(service))
        async with WeaveClient("http://127.0.0.1", transport=transport) as client:
            snapshot = await client.snapshot()
            messages = await client.messages()
            message = messages.items[-1]
            event = next(value for value in service.store.events().items if value.type == "chat.message.created")
            assert MessageView.model_validate(event.data) == message
            assert snapshot.messages.items[-1] == message
            assert message.reports[0].report_id == report.report_id
            assert message.reports[0].execution_id == execution.execution_id
            assert message.reports[0].artifact_ids == (artifact.artifact_id,)
            assert message.artifacts[0].artifact_id == artifact.artifact_id
            assert message.artifacts[0].name == "Evidence.md"
            assert message.artifacts[0].digest == hashlib.sha256(b"# Verified evidence\n").hexdigest()
            assert message.artifacts[0].deletion_state == "active"
            serialized = message.model_dump(mode="json")
            assert "relative_path" not in serialized["artifacts"][0]
            assert str(service.store.root) not in message.model_dump_json()
            before = service.store.messages(before_sequence=message.chat_sequence)
            assert before.items == ()

    asyncio.run(scenario())


def test_scheduler_storage_failure_closes_http_admission_and_readiness_before_accepting_more_chat(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        service = product_service(tmp_path, ProductScenarioModel())
        await service.start()
        scheduler = service.scheduler
        assert scheduler is not None
        app = create_app(service)
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8000") as client:
                snapshot = JSON_OBJECT.validate_json((await client.get("/v1/snapshot")).content)
                capabilities = snapshot["capabilities"]
                assert isinstance(capabilities, dict) and isinstance(capabilities["csrf_token"], str)
                with service.store.transaction(write=True) as connection:
                    connection.execute("DROP TABLE deliveries")
                service.wakeup.set()
                with pytest.raises(ExceptionGroup):
                    async with asyncio.timeout(5):
                        await scheduler
                assert not service.ready
                assert (await client.get("/healthz")).status_code == 200
                assert (await client.get("/readyz")).status_code == 503
                response = await client.post(
                    "/v1/chat/messages",
                    json={"client_message_id": "after-failure", "text": "status"},
                    headers={"X-Weave-CSRF": capabilities["csrf_token"]},
                )
                assert response.status_code == 503
                assert service.store.messages().total == 0
        finally:
            with pytest.raises(ExceptionGroup):
                await service.shutdown()
            assert service.owner_descriptor is None

    asyncio.run(scenario())


def test_execution_input_details_distinguish_sdk_consumption_from_returned_responsibility(tmp_path: Path) -> None:
    async def scenario() -> None:
        model = ProductScenarioModel()
        service = product_service(tmp_path, model)
        execution = create_execution(service.store, "input-evidence")
        accepted = service.store.submit_message(
            ChatMessageRequest(
                client_message_id="input-evidence",
                text="Investigate the request",
                target_execution_ids=(execution.execution_id,),
            )
        )
        item = service.store.get_input(accepted.input_ids[0])
        assert item is not None
        delivery = next(value for value in service.store.pending_deliveries() if value.input_id == item.input_id)
        service.store.fail_delivery(
            delivery.delivery_id, expected_generation=delivery.generation, error="Transport interrupted"
        )
        artifact = service.content.install_artifact(
            execution.execution_id,
            artifact_id="input-evidence",
            name="evidence.txt",
            mime="text/plain",
            payload=b"Actual owned evidence",
        )
        app = create_app(service)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8000") as client:
            path = f"/v1/executions/{execution.execution_id}"
            before = ExecutionDetailView.model_validate_json((await client.get(path)).content)
            assert before.inputs[0].sdk_consumption is None
            assert before.deliveries[0].available_actions == ("retry",)
            session = Session(execution.session_id, service.sessions)
            receipt = await session.enqueue(
                InputEnvelope(
                    input_id=item.input_id,
                    kind=item.kind,
                    source=item.source,
                    content=item.content,
                    causation_id=item.causation_id,
                )
            )
            service.store.confirm_delivery(
                delivery.delivery_id, expected_generation=delivery.generation, sdk_sequence=receipt.sequence
            )
            delivered = ExecutionDetailView.model_validate_json((await client.get(path)).content)
            assert delivered.inputs[0].sdk_consumption is not None
            assert delivered.inputs[0].sdk_consumption.state == "accepted"
            started = await service.sessions.start(
                session.session_id,
                invocation_id="consume-input",
                parameters_digest=hashlib.sha256(b"consume-input").hexdigest(),
                input_ids=(item.input_id,),
                input_mode="batch",
                agent_id="worker",
            )
            writer = SessionWriter(session, started.snapshot)
            step = await writer.begin("worker")
            consumed = ExecutionDetailView.model_validate_json((await client.get(path)).content)
            fact = consumed.inputs[0].sdk_consumption
            assert fact is not None and fact.state == "consumed"
            assert fact.run_id == writer.run_id and fact.step_id == step.step_id
            assert consumed.dispositions[0].state == "owned"
            assert consumed.session_read_sequence == (await service.sessions.snapshot(session.session_id)).revision
            handback = service.store.return_to_router(
                item.input_id,
                execution_id=execution.execution_id,
                expected_revision=1,
                reason="Needs a different Worker",
            )
            returned_response = await client.get(path)
            returned = ExecutionDetailView.model_validate_json(returned_response.content)
            assert returned.dispositions[0].state == "returned"
            assert returned.dispositions[0].handback_id == handback.handback_id
            assert returned.handbacks == (handback,)
            assert returned.inputs[0].sdk_consumption == fact
            assert returned.artifacts[0].artifact_id == artifact.artifact_id
            assert "relative_path" not in returned_response.text
            space_response = await client.get(f"/v1/spaces/{execution.space_id}")
            assert space_response.status_code == 200 and artifact.artifact_id in space_response.text
            assert "relative_path" not in space_response.text
        assert model.requests == []

    asyncio.run(scenario())


def test_graph_attempt_pages_reach_the_101st_real_node_and_keep_the_exact_root_sdk_identity(tmp_path: Path) -> None:
    async def scenario() -> None:
        service = product_service(tmp_path, ProductScenarioModel())
        execution = create_execution(service.store, "graph-pages", running=True)
        session = Session(execution.session_id, service.sessions)
        await session.enqueue(InputEnvelope(input_id="root", kind="user", source="host", content="Run the graph"))
        accepted = await service.sessions.start(
            session.session_id,
            invocation_id="graph-root",
            parameters_digest="a" * 64,
            input_ids=("root",),
            input_mode="batch",
            agent_id="worker",
        )
        service.store.record_execution_run(
            ExecutionRun(
                execution_id=execution.execution_id,
                attempt=2,
                invocation_id="graph-root",
                run_id=accepted.snapshot.run_id,
                state="running",
            )
        )
        model = NodeModel()
        runner = node_runner(model)
        controls = GraphControls(
            execution.execution_id, service.details, service.content, {"text": model}, runner, service.sessions
        )
        context = ToolContext(
            call_id="graph",
            run_id=accepted.snapshot.run_id,
            parent_limits=ParentRunLimits(
                deadline=datetime.now(UTC) + timedelta(seconds=60),
                remaining_model_steps=256,
                remaining_output_tokens=10_000,
                tool_concurrency=8,
            ),
        )
        spec = GraphSpec(
            graph_id="paged",
            nodes=tuple(
                NodeSpec(
                    node_id=f"node-{index}",
                    objective="Emit a confirmed object",
                    limits=RunLimits(max_model_steps=1, max_output_tokens=8),
                )
                for index in range(101)
            ),
        )
        submitted = await controls.submit(context, {"graph": spec.model_dump(mode="json")})
        assert submitted["accepted"] is True
        result = await controls.execute(context, {"graph_id": "paged", "input": {}})
        assert isinstance(result, dict) and isinstance(result["graph_run_id"], str)
        graph_run_id = result["graph_run_id"]
        app = create_app(service)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8000") as client:
            url = f"/v1/executions/{execution.execution_id}/graph-runs/{graph_run_id}"
            first = JSON_OBJECT.validate_json((await client.get(f"{url}?limit=100")).content)
            assert first["total"] == 101 and first["next_cursor"] == 100
            metadata = first["graph_run"]
            assert isinstance(metadata, dict) and metadata["root_run_id"] == accepted.snapshot.run_id
            remaining = JSON_OBJECT.validate_json((await client.get(f"{url}?cursor=100&limit=100")).content)
            attempts = remaining["node_attempts"]
            assert isinstance(attempts, list) and len(attempts) == 1 and isinstance(attempts[0], dict)
            assert remaining["next_cursor"] is None and remaining["total"] == 101
            identity = attempts[0]["node_attempt_id"]
            assert isinstance(identity, str)
            detail = await client.get(f"/v1/executions/{execution.execution_id}/node-attempts/{identity}")
            assert (
                detail.status_code == 200 and JSON_OBJECT.validate_json(detail.content)["node_attempt_id"] == identity
            )
            initial = first["node_attempts"]
            assert isinstance(initial, list) and all(
                isinstance(value, dict) and value["node_attempt_id"] != identity for value in initial
            )
        assert len(model.requests) == 101
        await runner.aclose()
        await service.agents.aclose()

    asyncio.run(scenario())
