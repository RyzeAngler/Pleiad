import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path

import pytest
from pleiad.contracts import JsonObject
from pleiad.models.providers import openai
from pydantic import SecretStr
from textual.app import App, ComposeResult
from textual.events import MouseScrollDown
from textual.widgets import Button, DataTable, Input, Static
from typer.testing import CliRunner

from pleiad_weave import cli
from pleiad_weave.agent.contracts import GraphSpec, NodeSpec
from pleiad_weave.application import service as service_module
from pleiad_weave.application.dto import ExecuteRequest
from pleiad_weave.application.events import ExecutionEvent
from pleiad_weave.application.service import ExecutionService
from pleiad_weave.infrastructure.bootstrap import create_application
from pleiad_weave.infrastructure.config import Settings
from pleiad_weave.infrastructure.storage.graphs import GraphRevision
from pleiad_weave.infrastructure.storage.models import ExecutionStatus
from pleiad_weave.tui.app import PleiadApp
from pleiad_weave.tui.log_bridge import TuiLogHandler, safe_log_message
from pleiad_weave.tui.projection import TuiProjection
from pleiad_weave.tui.screens.approvals import ApprovalScreen
from pleiad_weave.tui.screens.main import MainScreen
from pleiad_weave.tui.state import (
    ArchivedProcessGroupView,
    AssistantMessageView,
    LogRecordView,
    TraceEventView,
    TuiState,
    UserMessageView,
)
from pleiad_weave.tui.widgets.conversation import ConversationViewport
from pleiad_weave.tui.widgets.runtime import ProgramLogBuffer
from tests.test_execution import PINNED_IMAGE, ScriptedProvider


@pytest.mark.parametrize("restart", [False, True], ids=["same-process", "reopened-workspace"])
def test_graph_approval_keeps_the_inspected_revision_when_a_new_version_is_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, restart: bool
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "capabilities.yaml").write_text("tools: []\nskills: []\n", encoding="utf-8")

    def create(provider: ScriptedProvider) -> ExecutionService:
        def binding(**options: object) -> ScriptedProvider:
            return provider

        monkeypatch.setattr(openai, "OpenAIProvider", binding)
        return create_application(
            Settings(
                workspace=workspace,
                llm_provider="openai",
                llm_protocol="openai",
                llm_model="test-model",
                llm_api_key="synthetic",
                sandbox_image=PINNED_IMAGE,
                log_console=False,
                approval_tool_codes=frozenset({"weave.execute_graph"}),
            )
        )

    async def scenario() -> None:
        request: JsonObject = {"name": "execute_graph", "arguments": {"graph_id": "choice", "input": {}}}
        provider = ScriptedProvider((request,) if restart else (request, '{"version":"A"}', "done"))
        service = create(provider)
        try:
            service.weave.graph_store.submit(
                GraphSpec(graph_id="choice", nodes=(NodeSpec(node_id="one", objective="VERSION_A_APPROVED"),))
            )
            queued = await service.submit(ExecuteRequest(objective="Execute the selected graph"))
            waiting = await service.wait(queued.execution_id)
            assert waiting is not None and waiting.status is ExecutionStatus.WAITING_APPROVAL
            saved = service.repository.resume_state(queued.execution_id)
            assert saved is not None
            pending = (await service.approvals(queued.execution_id))[0]
            assert pending.interruption.arguments == {"graph_id": "choice", "input": {}}
            resolved = pending.interruption.details["graph"]
            assert isinstance(resolved, dict) and resolved["graph_revision"] == 1
            service.weave.graph_store.submit(
                GraphSpec(
                    graph_id="choice",
                    revision=2,
                    nodes=(NodeSpec(node_id="one", objective="VERSION_B_REPLACEMENT"),),
                )
            )
            if restart:
                await service.shutdown()
                provider = ScriptedProvider(('{"version":"A"}', "done"))
                service = create(provider)
                await service.start()
            app = PleiadApp(service)
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                screen = app.screen
                assert isinstance(screen, ApprovalScreen)
                details = str(screen.query_one("#approval-details", Static).render())
                assert '"graph_revision": 1' in details
                assert '"graph_revision": 2' not in details
                await pilot.click("#approval-approve")
                await pilot.pause()
                result = await service.wait(queued.execution_id)
                assert result is not None and result.status is ExecutionStatus.SUCCEEDED
                assert result.graph_id == "choice" and result.graph_revision == 1
                requests = "\n".join(item.model_dump_json() for item in provider.requests)
                assert "VERSION_A_APPROVED" in requests and "VERSION_B_REPLACEMENT" not in requests
                resumed = [
                    event for event in await service.events(queued.execution_id) if event.event_type == "run.resumed"
                ]
                assert len(resumed) == 1 and resumed[0].data["run_id"] == saved.checkpoint.run_id
        finally:
            await service.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("alteration", ["missing", "changed"])
def test_graph_approval_rejects_a_missing_or_changed_revision_without_running_the_latest_graph(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, alteration: str
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "capabilities.yaml").write_text("tools: []\nskills: []\n", encoding="utf-8")

    def create(provider: ScriptedProvider) -> ExecutionService:
        def binding(**options: object) -> ScriptedProvider:
            return provider

        monkeypatch.setattr(openai, "OpenAIProvider", binding)
        return create_application(
            Settings(
                workspace=workspace,
                llm_provider="openai",
                llm_protocol="openai",
                llm_model="test-model",
                llm_api_key="synthetic",
                sandbox_image=PINNED_IMAGE,
                log_console=False,
                approval_tool_codes=frozenset({"weave.execute_graph"}),
            )
        )

    async def scenario() -> None:
        request: JsonObject = {"name": "execute_graph", "arguments": {"graph_id": "choice", "input": {}}}
        first = create(ScriptedProvider((request,)))
        try:
            store = first.weave.graph_store
            original = store.submit(
                GraphSpec(graph_id="choice", nodes=(NodeSpec(node_id="one", objective="VERSION_A_APPROVED"),))
            )
            queued = await first.submit(ExecuteRequest(objective="Execute the selected graph"))
            waiting = await first.wait(queued.execution_id)
            assert waiting is not None and waiting.status is ExecutionStatus.WAITING_APPROVAL
            pending = (await first.approvals(queued.execution_id))[0]
            store.submit(
                GraphSpec(graph_id="choice", revision=2, nodes=(NodeSpec(node_id="one", objective="VERSION_B_LATEST"),))
            )
            if alteration == "missing":
                assert store.root is not None
                saved = tuple(store.root.glob("*/revision-1.json"))
                assert len(saved) == 1
                saved[0].unlink()
            else:
                store.persist(
                    GraphRevision(
                        graph=GraphSpec(
                            graph_id="choice", nodes=(NodeSpec(node_id="one", objective="VERSION_A_TAMPERED"),)
                        ),
                        created_at=original.created_at,
                    )
                )
        finally:
            await first.shutdown()
        provider = ScriptedProvider(())
        second = create(provider)
        try:
            latest = second.weave.graph_store.current("choice")
            assert latest is not None and latest.graph.revision == 2
            await second.start()
            await second.decide(queued.execution_id, pending.interruption.interruption_id, "approve")
            result = await second.wait(queued.execution_id)
            assert result is not None and result.status is ExecutionStatus.FAILED
            assert result.error is not None and result.error.code == "weave.approval_incompatible"
            assert "no longer available" in result.error.message or "definition changed" in result.error.message
            assert provider.requests == []
            assert second.weave.graph_executor.graphs.list_records() == ()
            assert second.weave.graph_executor.nodes.list_records() == ()
            assert await second.weave.memory.session("default").active() is None
        finally:
            await second.shutdown()

    asyncio.run(scenario())


class TuiWidgetApp(App[None]):
    """Mount the real workbench and stylesheet without external services."""

    CSS_PATH = Path(__file__).resolve().parents[1] / "src/tui/styles/theme.tcss"

    def compose(self) -> ComposeResult:
        """Build the production workbench for widget interaction tests."""
        yield MainScreen()


def test_program_log_redacts_credentials_before_rendering() -> None:
    assert safe_log_message("token=abc Authorization: Bearer xyz normal") == (
        "token=[REDACTED] Authorization=[REDACTED] normal"
    )


def test_answer_completion_updates_one_message_after_process_archive() -> None:
    state = TuiState()
    state.begin_execution("default")
    state.add_user_message("inspect the graph")
    state.append_process_summary("Analyzing request")
    state.start_answer()
    state.append_answer("partial")
    state.finish_answer()
    state.complete_answer("final answer")

    users = [item for item in state.conversation_items if isinstance(item, UserMessageView)]
    archives = [item for item in state.conversation_items if isinstance(item, ArchivedProcessGroupView)]
    answers = [item for item in state.conversation_items if isinstance(item, AssistantMessageView)]

    assert len(users) == 1
    assert len(archives) == 1
    assert archives[0].collapsed is True
    assert len(answers) == 1
    assert answers[0].content == "final answer"
    assert answers[0].streaming is False


def test_program_log_is_bounded_and_tracks_unread_records_when_paused() -> None:
    state = TuiState()
    timestamp = datetime.now().astimezone()
    for index in range(501):
        state.add_program_log(
            LogRecordView(
                record_id=f"log-{index}",
                timestamp=timestamp,
                level="INFO",
                message=f"line {index}",
                logger="pleiad_weave",
            )
        )

    state.set_program_log_follow(False)
    state.add_program_log(
        LogRecordView(
            record_id="log-501",
            timestamp=timestamp,
            level="WARNING",
            message="new line",
            logger="pleiad_weave",
        )
    )

    assert len(state.runtime.log_records) == 500
    assert state.runtime.log_records[0].record_id == "log-2"
    assert state.runtime.log_records[-1].record_id == "log-501"
    assert state.runtime.log_unread == 1


def test_trace_events_are_sorted_and_duplicate_sequences_are_ignored() -> None:
    state = TuiState()
    timestamp = datetime.now().astimezone()
    state.add_trace_event(TraceEventView(2, timestamp, "tool.started", "", "running", "execution-1"))
    state.add_trace_event(TraceEventView(1, timestamp, "turn.started", "", "running", "execution-1"))
    state.add_trace_event(TraceEventView(2, timestamp, "tool.completed", "", "completed", "execution-1"))

    assert [event.sequence for event in state.trace.events] == [1, 2]
    assert [event.event_type for event in state.trace.events] == ["turn.started", "tool.started"]


@pytest.mark.parametrize("size", [(120, 40), (80, 24)])
@pytest.mark.parametrize("interaction", ["wheel", "keyboard"])
def test_long_conversation_scrolls_content_and_scrollbar(size: tuple[int, int], interaction: str) -> None:
    async def scenario() -> None:
        state = TuiState()
        state.set_conversation_follow(False)
        for index in range(40):
            state.add_user_message(f"Message {index}\nSecond line\nThird line")
        app = TuiWidgetApp()
        async with app.run_test(size=size) as pilot:
            main = app.query_one(MainScreen)
            main.set_width(size[0])
            main.set_height(size[1])
            main.update_state(state)
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            viewport = app.query_one(ConversationViewport)
            assert viewport.max_scroll_y > 0
            assert viewport.scroll_y == 0
            first_message_y = viewport.children[0].region.y

            if interaction == "wheel":
                viewport.post_message(MouseScrollDown(viewport, 2, 2, 0, 1, 0, False, False, False))
            else:
                viewport.focus()
                await pilot.press("pagedown")
            await pilot.pause()
            await pilot.wait_for_scheduled_animations()

            assert viewport.scroll_y > 0
            assert viewport.children[0].region.y == first_message_y - round(viewport.scroll_y)
            assert viewport.vertical_scrollbar.position == round(viewport.scroll_y)
            assert state.conversation_follow is False

            paused_scroll_y = viewport.scroll_y
            state.add_user_message("New message while reading history")
            main.update_state(state)
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert viewport.scroll_y == paused_scroll_y

            viewport.focus()
            await pilot.press("end")
            await pilot.wait_for_scheduled_animations()
            await pilot.pause()
            assert viewport.scroll_y == viewport.max_scroll_y
            assert viewport.vertical_scrollbar.position == viewport.max_scroll_y
            assert state.conversation_follow is True

    asyncio.run(scenario())


@pytest.mark.parametrize("size", [(120, 40), (80, 24)])
@pytest.mark.parametrize("interaction", ["wheel", "keyboard"])
def test_long_program_log_scrolls_visible_records_and_scrollbar(size: tuple[int, int], interaction: str) -> None:
    async def scenario() -> None:
        state = TuiState()
        state.set_program_log_follow(False)
        timestamp = datetime.now().astimezone()
        for index in range(80):
            state.add_program_log(LogRecordView(str(index), timestamp, "INFO", f"LOG{index:02d}", "pleiad_weave"))
        app = TuiWidgetApp()
        async with app.run_test(size=size) as pilot:
            main = app.query_one(MainScreen)
            main.set_width(size[0])
            main.set_height(size[1])
            main.update_state(state)
            main.show_view("runtime")
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            buffer = app.query_one(ProgramLogBuffer)
            assert buffer.max_scroll_y > 0
            assert buffer.scroll_y == 0
            assert "LOG00" in app.export_screenshot()

            if interaction == "wheel":
                buffer.post_message(MouseScrollDown(buffer, 2, 2, 0, 1, 0, False, False, False))
            else:
                buffer.focus()
                await pilot.press("pagedown")
            await pilot.pause()
            await pilot.wait_for_scheduled_animations()

            assert 0 < buffer.scroll_y < buffer.max_scroll_y
            assert buffer.vertical_scrollbar.position == round(buffer.scroll_y)
            screenshot = app.export_screenshot()
            assert "LOG00" not in screenshot
            assert f"LOG{round(buffer.scroll_y) // 2:02d}" in screenshot
            assert state.runtime.log_follow is False

            paused_scroll_y = buffer.scroll_y
            state.add_program_log(LogRecordView("new", timestamp, "INFO", "NEW_RECORD", "pleiad_weave"))
            main.update_state(state)
            await pilot.pause()
            assert buffer.scroll_y == paused_scroll_y
            assert "NEW_RECORD" not in app.export_screenshot()

            buffer.focus()
            await pilot.press("end")
            await pilot.wait_for_scheduled_animations()
            await pilot.pause()
            assert buffer.scroll_y == buffer.max_scroll_y
            assert buffer.vertical_scrollbar.position == buffer.max_scroll_y
            assert "NEW_RECORD" in app.export_screenshot()
            assert state.runtime.log_follow is True

    asyncio.run(scenario())


@pytest.mark.parametrize("size", [(120, 40), (80, 24)])
@pytest.mark.parametrize("follow", [True, False])
@pytest.mark.parametrize("interaction", ["keyboard", "shift-wheel"])
def test_runtime_horizontal_scroll_survives_new_log_records(
    size: tuple[int, int], follow: bool, interaction: str
) -> None:
    async def scenario() -> None:
        state = TuiState()
        timestamp = datetime.now().astimezone()
        state.add_program_log(LogRecordView("first", timestamp, "INFO", "LEFT " + "x" * 130 + " RIGHT", "pleiad"))
        app = TuiWidgetApp()
        async with app.run_test(size=size) as pilot:
            main = app.query_one(MainScreen)
            main.set_width(size[0])
            main.set_height(size[1])
            main.update_state(state)
            main.show_view("runtime")
            await pilot.pause()
            buffer = app.query_one(ProgramLogBuffer)
            buffer.focus()
            if interaction == "keyboard":
                await pilot.press("right")
            else:
                buffer.post_message(MouseScrollDown(buffer, 2, 2, 0, 1, 0, True, False, False))
                await pilot.pause()
            await pilot.wait_for_scheduled_animations()
            assert buffer.scroll_x > 0
            buffer.scroll_to(x=buffer.max_scroll_x, animate=False, immediate=True)
            await pilot.pause()
            saved_scroll_x = buffer.scroll_x
            assert "RIGHT" in app.export_screenshot()

            state.set_program_log_follow(follow)
            state.add_program_log(LogRecordView("next", timestamp, "INFO", "Another record", "pleiad"))
            main.update_state(state)
            await pilot.pause()
            assert buffer.scroll_x == saved_scroll_x
            assert buffer.horizontal_scrollbar.position == round(saved_scroll_x)
            assert "RIGHT" in app.export_screenshot()

    asyncio.run(scenario())


@pytest.mark.parametrize("size", [(120, 40), (80, 24)])
def test_short_runtime_logs_fit_without_horizontal_scroll(size: tuple[int, int]) -> None:
    async def scenario() -> None:
        state = TuiState()
        state.add_program_log(LogRecordView("first", datetime.now().astimezone(), "INFO", "Ready", "pleiad"))
        app = TuiWidgetApp()
        async with app.run_test(size=size) as pilot:
            main = app.query_one(MainScreen)
            main.set_width(size[0])
            main.set_height(size[1])
            main.update_state(state)
            main.show_view("runtime")
            await pilot.pause()
            buffer = app.query_one(ProgramLogBuffer)
            assert buffer.max_scroll_x == 0
            assert "Ready" in app.export_screenshot()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("run started run=run-1 agent=weave-supervisor", "主助手开始处理任务"),
        ("run ended run=run-1 status=succeeded", "智能体运行完成"),
        ("run ended run=run-1 status=failed", "智能体运行失败"),
        (
            "file write call_id=call-1 path=reports/my report.txt bytes=953 mode=create",
            "写入文件\uff1areports/my report.txt\uff08953 字节\uff09",
        ),
        (
            "file read call_id=call-1 scope=workspace path=README.md bytes=120 line_start=1 line_end=5 truncated=False",
            "读取文件\uff1aREADME.md\uff08120 字节\uff09",
        ),
        (
            "command completed call_id=call-1 backend=docker exit_code=0 timed_out=False output_truncated=False",
            "命令执行成功\uff08docker\uff09",
        ),
        (
            "command completed call_id=call-1 backend=docker exit_code=1 timed_out=False output_truncated=False",
            "命令执行失败\uff08退出码 1\uff09",
        ),
        (
            "command completed call_id=call-1 backend=docker exit_code=None timed_out=True output_truncated=True",
            "命令执行超时\uff1b输出已截断",
        ),
    ],
)
def test_program_log_explains_operations_without_exposing_run_ids(message: str, expected: str) -> None:
    records: list[LogRecordView] = []
    record = logging.LogRecord("pleiad", logging.INFO, __file__, 1, message, (), None)
    record.__dict__["execution_id"] = "execution-1"
    TuiLogHandler(records.append).emit(record)

    assert len(records) == 1
    assert records[0].message == expected
    assert records[0].execution_id == "execution-1"


def test_unrecognized_program_log_preserves_redacted_diagnostics() -> None:
    records: list[LogRecordView] = []
    record = logging.LogRecord("pleiad", logging.ERROR, __file__, 1, "Connection refused token=secret", (), None)
    TuiLogHandler(records.append).emit(record)

    assert records[0].message == "Connection refused token=[REDACTED]"
    assert records[0].level == "ERROR"


@pytest.mark.parametrize("size", [(120, 40), (80, 24)])
def test_trace_displays_aligned_columns_and_explains_failed_operations_in_chinese(size: tuple[int, int]) -> None:
    async def scenario() -> None:
        state = TuiState()
        projection = TuiProjection(state)
        projection.apply(
            ExecutionEvent.create("execution-1", "run.started", {"agent_name": "weave-supervisor"}, sequence=1)
        )
        projection.apply(
            ExecutionEvent.create(
                "execution-1",
                "tool.completed",
                {"tool_name": "read_file", "ok": False, "elapsed_ms": 42, "error": {"code": "tool.not_found"}},
                sequence=2,
            )
        )
        projection.apply(ExecutionEvent.create("execution-1", "run.timed_out", sequence=3))
        app = TuiWidgetApp()
        async with app.run_test(size=size) as pilot:
            main = app.query_one(MainScreen)
            main.set_width(size[0])
            main.set_height(size[1])
            main.update_state(state)
            main.show_view("trace")
            await pilot.pause()
            table = app.query_one("#trace-log", DataTable)
            assert [column.label.plain for column in table.columns.values()] == ["事件", "状态", "时间", "序号", "说明"]
            assert table.get_row("1")[0].plain == "智能体启动"
            assert table.get_row("2")[1].plain == "失败"
            assert table.get_row("2")[4].plain == "read_file · tool.not_found · 耗时 42 毫秒"
            assert table.get_row("3")[1].plain == "超时"
            screenshot = app.export_screenshot()
            assert "失败" in screenshot
            assert "超时" in screenshot
            widths = [column.width for column in table.columns.values()][:4]

            state.add_trace_event(
                TraceEventView(
                    4, datetime.now().astimezone(), "extension." + "x" * 100, "扩展信息", "custom", "execution-1"
                )
            )
            main.update_state(state)
            await pilot.pause()
            assert [column.width for column in table.columns.values()][:4] == widths
            assert "custom" in table.get_row("4")[1].plain

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("event_type", "status"),
    [
        ("execution.queued", "queued"),
        ("execution.waiting", "waiting"),
        ("run.timed_out", "timed_out"),
        ("run.limit_reached", "limit_reached"),
    ],
)
def test_trace_distinguishes_waiting_timeouts_and_limits_from_success(event_type: str, status: str) -> None:
    state = TuiState()
    projection = TuiProjection(state)
    projection.apply(ExecutionEvent.create("execution-1", event_type, sequence=1))

    assert state.trace.events[0].status == status


@pytest.mark.parametrize("size", [(120, 40), (80, 24)])
def test_trace_preserves_reading_position_when_an_event_is_appended(size: tuple[int, int]) -> None:
    async def scenario() -> None:
        state = TuiState()
        timestamp = datetime.now().astimezone()
        for sequence in range(1, 81):
            state.add_trace_event(
                TraceEventView(sequence, timestamp, "tool.started", "详情" * 80, "running", "execution-1")
            )
        app = TuiWidgetApp()
        async with app.run_test(size=size) as pilot:
            main = app.query_one(MainScreen)
            main.set_width(size[0])
            main.set_height(size[1])
            main.update_state(state)
            main.show_view("trace")
            await pilot.pause()
            table = app.query_one("#trace-log", DataTable)
            table.move_cursor(row=15, scroll=False)
            table.scroll_to(x=20, y=10, animate=False, immediate=True)
            await pilot.pause()
            assert table.scroll_x == 20
            assert table.scroll_y == 10

            state.add_trace_event(TraceEventView(81, timestamp, "tool.completed", "完成", "completed", "execution-1"))
            main.update_state(state)
            await pilot.pause()
            assert table.scroll_x == 20
            assert table.scroll_y == 10
            assert table.cursor_row == 15
            assert table.row_count == 81

    asyncio.run(scenario())


def create_approval_service(
    workspace: Path, provider: ScriptedProvider, monkeypatch: pytest.MonkeyPatch
) -> ExecutionService:
    """Compose the real product with an explicit approval policy and fake model boundary."""
    workspace.mkdir(exist_ok=True)
    (workspace / "capabilities.yaml").write_text("tools: []\nskills: []\n", encoding="utf-8")

    def bind_provider(**options: object) -> ScriptedProvider:
        return provider

    monkeypatch.setattr(openai, "OpenAIProvider", bind_provider)
    return create_application(
        Settings(
            workspace=workspace,
            llm_provider="openai",
            llm_model="test-model",
            llm_api_key=SecretStr("test-key"),
            llm_vision_model=None,
            llm_speech_model=None,
            log_console=False,
            sandbox_image=PINNED_IMAGE,
            approval_tool_codes=frozenset({"write_file"}),
        )
    )


@pytest.mark.parametrize("size", [(120, 40), (80, 24)])
@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_tui_buttons_decide_the_exact_pending_action_through_the_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, size: tuple[int, int], decision: str
) -> None:
    async def scenario() -> None:
        workspace = tmp_path / "workspace"
        provider = ScriptedProvider(
            ({"name": "write_file", "arguments": {"path": "created.txt", "content": "approved content"}}, "continued")
        )
        service = create_approval_service(workspace, provider, monkeypatch)
        app = PleiadApp(service)
        async with app.run_test(size=size) as pilot:
            app.query_one("#composer-input", Input).value = "Create created.txt"
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert isinstance(app.screen, ApprovalScreen)
            details = str(app.screen.query_one("#approval-details", Static).renderable)
            assert "write_file" in details and "created.txt" in details and "approved content" in details
            assert any(event.event_type == "execution.waiting_approval" for event in app.state.trace.events)
            assert not (workspace / "created.txt").exists()
            await pilot.click(f"#approval-{decision}")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.state.execution_status.value == "succeeded"
            assert app.state.execution_id is not None
            record = service.get(app.state.execution_id)
            assert record is not None and record.output == {"text": "continued"}
            assert await service.approvals(app.state.execution_id) == ()
            if decision == "approve":
                assert (workspace / "created.txt").read_text(encoding="utf-8") == "approved content"
            else:
                assert not (workspace / "created.txt").exists()

    asyncio.run(scenario())


def test_tui_selects_and_rejects_a_restored_request_without_deciding_another_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        workspace = tmp_path / "workspace"
        first = create_approval_service(
            workspace,
            ScriptedProvider(
                (
                    {"name": "write_file", "arguments": {"path": "first.txt", "content": "first"}},
                    {"name": "write_file", "arguments": {"path": "second.txt", "content": "second"}},
                )
            ),
            monkeypatch,
        )
        execution_ids: list[str] = []
        try:
            for conversation_id in ("one", "two"):
                queued = await first.submit(ExecuteRequest(objective="Create a file", conversation_id=conversation_id))
                record = await first.wait(queued.execution_id)
                assert record is not None and record.status is ExecutionStatus.WAITING_APPROVAL
                execution_ids.append(record.execution_id)
        finally:
            await first.shutdown()
        restarted = create_approval_service(workspace, ScriptedProvider(("Rejected selected action",)), monkeypatch)
        app = PleiadApp(restarted)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            assert isinstance(app.screen, ApprovalScreen)
            table = app.screen.query_one("#approval-list", DataTable)
            assert table.row_count == 2
            await pilot.press("down")
            await pilot.pause()
            selected_execution_id = str(table.get_row_at(1)[0])
            await pilot.press("r")
            await app.workers.wait_for_complete()
            await pilot.pause()
            selected = restarted.get(selected_execution_id)
            assert selected is not None and selected.status is ExecutionStatus.SUCCEEDED
            remaining = await restarted.approvals()
            assert len(remaining) == 1
            assert remaining[0].execution_id != selected_execution_id
            assert {remaining[0].execution_id, selected_execution_id} == set(execution_ids)
            assert not (workspace / "first.txt").exists() and not (workspace / "second.txt").exists()

    asyncio.run(scenario())


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_cli_lists_and_decides_a_persisted_request_using_the_shared_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, decision: str
) -> None:
    workspace = tmp_path / "workspace"

    async def prepare() -> str:
        first = create_approval_service(
            workspace,
            ScriptedProvider(({"name": "write_file", "arguments": {"path": "cli.txt", "content": "saved"}},)),
            monkeypatch,
        )
        try:
            queued = await first.submit(ExecuteRequest(objective="Create cli.txt"))
            waiting = await first.wait(queued.execution_id)
            assert waiting is not None and waiting.status is ExecutionStatus.WAITING_APPROVAL
            return queued.execution_id
        finally:
            await first.shutdown()

    execution_id = asyncio.run(prepare())
    monkeypatch.setattr(
        cli, "create_application", lambda: create_approval_service(workspace, ScriptedProvider(("Saved",)), monkeypatch)
    )
    runner = CliRunner()
    listed = runner.invoke(cli.app, ["approval", "list", "--workspace", str(workspace), "--execution-id", execution_id])
    assert listed.exit_code == 0, listed.output
    payload = json.loads(listed.stdout)
    assert payload[0]["execution_id"] == execution_id
    assert payload[0]["interruption"]["arguments"] == {"path": "cli.txt", "content": "saved"}
    decided = runner.invoke(
        cli.app, ["approval", decision, execution_id, payload[0]["interruption"]["interruption_id"]]
    )
    assert decided.exit_code == 0, decided.output
    record = json.loads(decided.stdout)
    assert record["execution_id"] == execution_id and record["status"] == "succeeded"
    if decision == "approve":
        assert (workspace / "cli.txt").read_text(encoding="utf-8") == "saved"
    else:
        assert not (workspace / "cli.txt").exists()


def test_tui_can_close_and_reopen_pending_approvals_without_deciding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        workspace = tmp_path / "workspace"
        service = create_approval_service(
            workspace,
            ScriptedProvider(({"name": "write_file", "arguments": {"path": "pending.txt", "content": "pending"}},)),
            monkeypatch,
        )
        app = PleiadApp(service)
        async with app.run_test(size=(80, 24)) as pilot:
            app.query_one("#composer-input", Input).value = "Create pending.txt"
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert isinstance(app.screen, ApprovalScreen)
            await pilot.press("escape")
            await pilot.pause()
            assert all(not isinstance(screen, ApprovalScreen) for screen in app.screen_stack)
            app.main_view.query_one("#composer-input", Input).value = "/approvals"
            app.main_view.query_one("#composer-input", Input).focus()
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert isinstance(app.screen, ApprovalScreen)
            pending = await service.approvals()
            assert len(pending) == 1
            record = service.get(pending[0].execution_id)
            assert record is not None and record.status is ExecutionStatus.WAITING_APPROVAL
            assert not (workspace / "pending.txt").exists()

    asyncio.run(scenario())


def test_cli_approval_commands_preserve_jobs_owned_by_a_live_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        workspace = tmp_path / "workspace"
        provider = ScriptedProvider(
            ({"name": "write_file", "arguments": {"path": "pending.txt", "content": "pending"}}, "active")
        )
        service = create_approval_service(workspace, provider, monkeypatch)

        def forbidden_application() -> ExecutionService:
            raise AssertionError("Listing approvals must not construct a second application")

        monkeypatch.setattr(cli, "create_application", forbidden_application)
        try:
            queued = await service.submit(ExecuteRequest(objective="Write pending.txt", conversation_id="pending"))
            waiting = await service.wait(queued.execution_id)
            assert waiting is not None and waiting.status is ExecutionStatus.WAITING_APPROVAL
            provider.block = True
            provider.started.clear()
            active = await service.submit(ExecuteRequest(objective="Keep running", conversation_id="active"))
            await asyncio.wait_for(provider.started.wait(), timeout=1)
            before = service.get(active.execution_id)
            assert before is not None and before.status is ExecutionStatus.RUNNING
            result = CliRunner().invoke(cli.app, ["approval", "list", "--workspace", str(workspace)])
            assert result.exit_code == 0, result.output
            payload = json.loads(result.stdout)
            assert [item["execution_id"] for item in payload] == [queued.execution_id]
            assert service.get(active.execution_id) == before
            monkeypatch.setattr(
                cli,
                "create_application",
                lambda: create_approval_service(workspace, ScriptedProvider(()), monkeypatch),
            )
            decided = await asyncio.to_thread(
                CliRunner().invoke,
                cli.app,
                ["approval", "approve", queued.execution_id, payload[0]["interruption"]["interruption_id"]],
            )
            assert decided.exit_code == 1
            assert "Use that service's API or TUI" in decided.output
            assert service.get(active.execution_id) == before
            pending = service.get(queued.execution_id)
            assert pending is not None and pending.status is ExecutionStatus.WAITING_APPROVAL
            assert not service.closed and not provider.closed
        finally:
            await service.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("input_json", ["{", "[]", "null", '"text"', '{"amount":NaN}', '{"amount":Infinity}'])
def test_cli_rejects_invalid_business_input_before_creating_a_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, input_json: str
) -> None:
    monkeypatch.setenv("PLEIAD_WORKSPACE", str(tmp_path / "workspace"))
    monkeypatch.setenv("PLEIAD_SANDBOX_IMAGE", PINNED_IMAGE)
    monkeypatch.setenv("PLEIAD_LLM_PROVIDER", "openai")
    monkeypatch.setenv("PLEIAD_LLM_PROTOCOL", "openai")
    monkeypatch.setenv("PLEIAD_LLM_API_KEY", "synthetic")

    def forbidden_provider(**options: object) -> ScriptedProvider:
        raise AssertionError("Invalid CLI input must not create a provider client")

    monkeypatch.setattr(openai, "OpenAIProvider", forbidden_provider)
    result = CliRunner().invoke(cli.app, ["run", "Read files", "--input", input_json])

    assert result.exit_code == 2
    assert "valid JSON object" in result.output


def test_cli_run_closes_its_provider_when_another_service_owns_the_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        workspace = tmp_path / "workspace"
        owner = create_approval_service(workspace, ScriptedProvider(()), monkeypatch)
        await owner.start()
        rejected_provider = ScriptedProvider(())
        rejected = create_approval_service(workspace, rejected_provider, monkeypatch)
        monkeypatch.setattr(cli, "create_application", lambda: rejected)
        try:
            result = await asyncio.to_thread(CliRunner().invoke, cli.app, ["run", "Read files"])
            assert result.exit_code == 1
            assert "Use that service's API or TUI" in result.output
            assert rejected.closed and rejected_provider.closed
            assert not owner.closed
        finally:
            await rejected.shutdown()
            await owner.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("settlement", ["cancel", "expire", "approve"])
def test_open_approval_screen_removes_an_action_settled_by_another_entry_point(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, settlement: str
) -> None:
    async def scenario() -> None:
        workspace = tmp_path / "workspace"
        provider = ScriptedProvider(
            ({"name": "write_file", "arguments": {"path": "pending.txt", "content": "approved"}}, "Completed")
        )
        service = create_approval_service(workspace, provider, monkeypatch)
        queued = await service.submit(ExecuteRequest(objective="Create pending.txt"))
        await service.wait(queued.execution_id)
        pending = await service.approvals(queued.execution_id)
        app = PleiadApp(service)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            assert isinstance(app.screen, ApprovalScreen)
            if settlement == "cancel":
                await service.cancel(queued.execution_id)
                expected_status = ExecutionStatus.CANCELLED
            elif settlement == "expire":
                future = datetime.now(UTC) + timedelta(hours=1)

                class ExpiredClock:
                    @staticmethod
                    def now(tz: tzinfo | None = None) -> datetime:
                        return future

                monkeypatch.setattr(service_module, "datetime", ExpiredClock)
                await service.expire(queued.execution_id)
                expected_status = ExecutionStatus.FAILED
            else:
                await service.decide(queued.execution_id, pending[0].interruption.interruption_id, "approve")
                await service.wait(queued.execution_id)
                expected_status = ExecutionStatus.SUCCEEDED
            await pilot.pause()
            assert all(not isinstance(screen, ApprovalScreen) for screen in app.screen_stack)
            assert await service.approvals(queued.execution_id) == ()
            record = service.get(queued.execution_id)
            assert record is not None and record.status is expected_status
            if settlement == "approve":
                assert (workspace / "pending.txt").read_text(encoding="utf-8") == "approved"
            else:
                assert not (workspace / "pending.txt").exists()

    asyncio.run(scenario())


@pytest.mark.parametrize("remove_selected", [False, True], ids=["other-action-settles", "selected-action-settles"])
def test_approval_refresh_preserves_focus_only_for_a_surviving_selected_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, remove_selected: bool
) -> None:
    async def scenario() -> None:
        workspace = tmp_path / "workspace"
        service = create_approval_service(
            workspace,
            ScriptedProvider(
                (
                    {"name": "write_file", "arguments": {"path": "first.txt", "content": "first"}},
                    {"name": "write_file", "arguments": {"path": "second.txt", "content": "second"}},
                )
            ),
            monkeypatch,
        )
        for conversation_id in ("first", "second"):
            queued = await service.submit(ExecuteRequest(objective="Create a file", conversation_id=conversation_id))
            await service.wait(queued.execution_id)
        app = PleiadApp(service)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert isinstance(app.screen, ApprovalScreen)
            screen = app.screen
            table = screen.query_one("#approval-list", DataTable)
            first_execution_id = str(table.get_row_at(0)[0])
            await pilot.press("down")
            await pilot.pause()
            selected_execution_id = str(table.get_row_at(table.cursor_row)[0])
            reject = screen.query_one("#approval-reject", Button)
            reject.focus()
            removed_execution_id = selected_execution_id if remove_selected else first_execution_id
            await service.cancel(removed_execution_id)
            await pilot.pause()
            assert table.row_count == 1
            surviving_execution_id = first_execution_id if remove_selected else selected_execution_id
            assert str(table.get_row_at(table.cursor_row)[0]) == surviving_execution_id
            assert app.focused is (table if remove_selected else reject)
            assert app.screen is screen
            assert await service.approvals(removed_execution_id) == ()

    asyncio.run(scenario())
