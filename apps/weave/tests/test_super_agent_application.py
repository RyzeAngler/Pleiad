import asyncio
import hashlib
import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import Gauge, Histogram, InMemoryMetricReader, Sum
from pleiad.contracts import JsonObject
from pleiad.messages.message import AssistantMessage, ToolCall
from pleiad.messages.results import ResultReference
from pleiad.models.binding import binding_fingerprint
from pleiad.models.openai.client import OpenAIModelClient
from pleiad.models.openai.request import OpenAIRequest
from pleiad.models.openai.response import (
    OpenAIStreamChoice,
    OpenAIStreamChunk,
    OpenAIStreamDelta,
    OpenAIStreamFunction,
    OpenAIStreamToolCall,
    OpenAIUsage,
)
from pleiad.observability.tracing import Observability
from pleiad.sandbox.backends.oci import OciSandbox
from pleiad.sandbox.models import SandboxReceipt, SandboxRequest, SandboxResult
from pleiad.sandbox.policy import SandboxPolicy
from pleiad.sandbox.sandbox import Sandbox
from pleiad.sessions.backends.sqlite import SqliteSessionStore
from pleiad.sessions.records import ToolCallRecord
from pleiad.sessions.recovery import ActionEvidence, ActionRef
from pleiad.sessions.session import Session
from pleiad.sessions.writer import SessionWriter
from pleiad.tools.contracts import ToolCall as RuntimeToolCall
from pleiad.tools.contracts import ToolContext, ToolResult
from pleiad.tools.errors import ToolFileAccessDeniedError, ToolFileNotFoundError
from pydantic import SecretStr, TypeAdapter
from typer.testing import CliRunner

from pleiad_weave.agent.weave import WeaveAgents
from pleiad_weave.application.service import ExecutionService
from pleiad_weave.application.verification import verify_delivery
from pleiad_weave.infrastructure.storage.memory import FactStore, MemoryFact
from pleiad_weave.infrastructure.storage.model_inputs import MODEL_INPUT_QUEUE_BYTES, ROUTER_BATCH_SOURCE_BYTES
from pleiad_weave.infrastructure.storage.product import ProductStore, fingerprint
from pleiad_weave.infrastructure.storage.product_content import ProductContent
from pleiad_weave.infrastructure.storage.product_details import ProductDetails
from pleiad_weave.infrastructure.storage.product_models import (
    ArtifactVerification,
    Attention,
    ChatMessageRequest,
    DeliveryContract,
    ExecutionRun,
    ExecutionSpec,
    ExecutionStatus,
    ProductConflict,
    ResolutionRequest,
    RouteAttempt,
    SpaceConfiguration,
    ToolVerification,
)
from pleiad_weave.memory_cli import memory_app

JSON_OBJECT = TypeAdapter[JsonObject](JsonObject)


class ProductScenarioModel(OpenAIModelClient):
    def __init__(self) -> None:
        self.provider = "fake"
        self.image_input = "unverified"
        self.api_key = SecretStr("")
        self.model = "scenario-model"
        self.binding_fingerprint = binding_fingerprint(
            protocol="openai", provider=self.provider, model=self.model, base_url=None, provider_options={}
        )
        self.requests: list[OpenAIRequest] = []
        self.workers_started = 0
        self.workers_ready = asyncio.Event()
        self.release_workers = asyncio.Event()
        self.status_answered = asyncio.Event()

    async def stream(self, request: OpenAIRequest | Mapping[str, object]) -> AsyncIterator[OpenAIStreamChunk]:
        if not isinstance(request, OpenAIRequest):
            raise TypeError("The scenario model requires the real parsed protocol request")
        self.requests.append(request)
        texts: list[str] = []
        for message in request.messages:
            if isinstance(message.content, str):
                texts.append(message.content)
            elif isinstance(message.content, list):
                for part in message.content:
                    if isinstance(part, dict):
                        text = part.get("text")
                        if isinstance(text, str):
                            texts.append(text)
        joined = "\n".join(texts)
        if any(tool.function.name == "query_product" for tool in request.tools or []):
            start = joined.index('{"inputs":')
            decoded: object = json.JSONDecoder().raw_decode(joined[start:])[0]
            data = JSON_OBJECT.validate_python(decoded)
            inputs = data["inputs"]
            if not isinstance(inputs, list) or not inputs or not isinstance(inputs[0], dict):
                raise ValueError("The routing context has no product inputs")
            batch_ids = data["source_input_ids"]
            if not isinstance(batch_ids, list):
                raise ValueError("The routing context has no fixed batch")
            origin = next(
                item
                for item in inputs
                if isinstance(item, dict) and item.get("input_id") in batch_ids and item.get("wake_policy") == "wake"
            )
            if not isinstance(origin, dict):
                raise ValueError("The routing source must be an object")
            content = origin["content"]
            identity = origin["input_id"]
            if content == "two objectives":
                output = {
                    "reply": "Working on both objectives.",
                    "actions": [
                        {
                            "kind": "create_space",
                            "local_ref": "general",
                            "configuration": {"name": "General", "purpose": "Execute work"},
                        },
                        {
                            "kind": "dispatch",
                            "space_ref": {"kind": "created", "id": "general"},
                            "objective": "first",
                            "source_input_ids": [identity],
                        },
                        {
                            "kind": "dispatch",
                            "space_ref": {"kind": "created", "id": "general"},
                            "objective": "second",
                            "source_input_ids": [identity],
                        },
                    ],
                }
            elif content == "clarify with attachment":
                output = {
                    "reply": "Which audience should the brief address?",
                    "actions": [
                        {
                            "kind": "request_clarification",
                            "question": "Which audience should the brief address?",
                            "source_input_ids": [identity],
                        }
                    ],
                }
            elif origin["source"] == "weave.attention":
                output = {
                    "reply": "Preparing the brief for the engineering team.",
                    "actions": [
                        {"kind": "create_space", "local_ref": "brief", "configuration": {"name": "Brief"}},
                        {
                            "kind": "dispatch",
                            "space_ref": {"kind": "created", "id": "brief"},
                            "objective": "Prepare the attached brief for the engineering team",
                            "source_input_ids": [identity],
                        },
                    ],
                }
            elif content == "status":
                self.status_answered.set()
                output = {"reply": "Both Workers are running.", "actions": []}
            else:
                output = {"reply": "The saved Worker report is ready.", "actions": []}
        else:
            self.workers_started += 1
            if self.workers_started == 2:
                self.workers_ready.set()
            await self.release_workers.wait()
            scope = re.search(r'\{"run_id":.*?"run_input_ids":\s*(\[[^\]]*\])', joined)
            if scope is None:
                raise ValueError("The Worker request is missing its SDK input scope")
            identities = TypeAdapter[list[str]](list[str]).validate_json(scope.group(1))
            output = {"summary": "Completed the requested work.", "handled_input_ids": identities}
        yield OpenAIStreamChunk(
            id=f"response-{len(self.requests)}",
            model=self.model,
            choices=(
                OpenAIStreamChoice(
                    index=0, delta=OpenAIStreamDelta(role="assistant", content=json.dumps(output)), finish_reason="stop"
                ),
            ),
            usage=OpenAIUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        )

    async def aclose(self) -> None:
        """Release no external transport in this deterministic provider boundary."""


def product_service(
    tmp_path: Path, model: ProductScenarioModel, *, observability: Observability | None = None
) -> ExecutionService:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    manifest = workspace / "capabilities.yaml"
    manifest.write_text("tools: []\nskills: []\n", encoding="utf-8")
    store = ProductStore(workspace / "runtime-v2")
    sessions = SqliteSessionStore(store.root / "sessions.sqlite3")
    content = ProductContent(store)
    details = ProductDetails(store)
    agents = WeaveAgents(
        {"text": model},
        workspace,
        manifest,
        Sandbox(OciSandbox()),
        SandboxPolicy(image=f"python@sha256:{'a' * 64}", allow_shell_command=True),
        store,
        sessions,
        content,
        details,
        Observability() if observability is None else observability,
    )
    return ExecutionService(agents, store, sessions, content, details)


def test_two_real_roots_continue_while_the_router_answers_a_status_query(tmp_path: Path) -> None:
    async def scenario() -> None:
        model = ProductScenarioModel()
        service = product_service(tmp_path, model)
        await service.start()
        try:
            await service.submit(ChatMessageRequest(client_message_id="two", text="two objectives"))
            async with asyncio.timeout(5):
                await model.workers_ready.wait()
            running = await asyncio.to_thread(service.store.list_executions)
            assert len(running.items) == 2
            assert all(
                value.parent_execution_id is None and value.status is ExecutionStatus.RUNNING for value in running.items
            )
            assert len({value.session_id for value in running.items}) == 2
            receipt = await service.submit(ChatMessageRequest(client_message_id="status", text="status"))
            async with asyncio.timeout(5):
                await model.status_answered.wait()
                while True:
                    committed = await asyncio.to_thread(service.store.messages)
                    if any(receipt.message_id in value.source_message_ids for value in committed.items):
                        break
            assert not model.release_workers.is_set()
            assert len((await asyncio.to_thread(service.store.list_executions)).items) == 2
            model.release_workers.set()
            async with asyncio.timeout(5):
                while True:
                    jobs = await asyncio.to_thread(service.store.list_executions)
                    if all(value.status is ExecutionStatus.SUCCEEDED for value in jobs.items):
                        break
            messages = await asyncio.to_thread(service.store.messages)
            assert [value.body for value in messages.items if value.role == "user"] == ["two objectives", "status"]
            assert any(value.body == "Both Workers are running." for value in messages.items)
        finally:
            model.release_workers.set()
            await service.shutdown()

    asyncio.run(scenario())


def test_global_clarification_survives_restart_and_assigns_original_material_with_the_answer(tmp_path: Path) -> None:
    async def scenario() -> None:
        model = ProductScenarioModel()
        service = product_service(tmp_path, model)
        attachment = service.content.upload_attachment(
            client_attachment_id="brief", name="brief.txt", mime="text/plain", payload=b"Original brief"
        )
        await service.start()
        receipt = await service.submit(
            ChatMessageRequest(
                client_message_id="ambiguous",
                text="clarify with attachment",
                attachment_ids=(attachment.attachment_id,),
            )
        )
        try:
            async with asyncio.timeout(5):
                while not service.store.attentions().items:
                    await asyncio.to_thread(service.store.snapshot)
            question = service.store.attentions().items[0]
            assert question.execution_id is None
            assert question.source_input_ids == receipt.input_ids
            assert service.store.list_executions().total == 0
        finally:
            await service.shutdown()
        service = product_service(tmp_path, model)
        await service.start()
        try:
            restored = service.store.get_attention(question.attention_id)
            assert restored is not None and restored.status == "pending"
            answer = await service.resolve(
                restored.attention_id,
                ResolutionRequest(
                    request_id="audience", expected_revision=restored.revision, answer="Engineering team"
                ),
            )
            async with asyncio.timeout(5):
                while True:
                    executions = await asyncio.to_thread(service.store.list_executions)
                    if model.workers_started == 1:
                        break
            execution = executions.items[0]
            binding = service.bindings[execution.execution_id]
            materialized = await binding.runner.tools.load("weave.execution_content").execute(
                ToolContext(call_id="brief", run_id="verify"),
                {"action": "materialize", "kind": "attachment", "id": attachment.attachment_id},
            )
            assert isinstance(materialized, dict) and isinstance(materialized["path"], str)
            work_root = service.agents.workspace / "work" / execution.space_id / execution.execution_id
            assert (work_root / materialized["path"]).read_bytes() == b"Original brief"
            assigned = service.store.worker_inputs(execution.execution_id, pending_only=False)
            assert {value.causation_id for value in assigned} == {receipt.message_id, answer.message_id}
            model.release_workers.set()
            async with asyncio.timeout(5):
                completed = await service.wait(execution.execution_id)
            assert completed.status is ExecutionStatus.SUCCEEDED
            for item in assigned:
                disposition = service.store.get_disposition(item.origin_input_id, execution.execution_id)
                assert disposition is not None and disposition.state == "handled"
            assert len(service.store.execution_runs(execution.execution_id)) == 1
            assert [value.body for value in service.store.messages().items if value.role == "user"] == [
                "clarify with attachment",
                "Engineering team",
            ]
        finally:
            model.release_workers.set()
            await service.shutdown()

    asyncio.run(scenario())


class VerificationScenarioModel(ProductScenarioModel):
    def __init__(self, command: str) -> None:
        super().__init__()
        self.command = command
        self.check_sent = False
        self.release_workers.set()

    async def stream(self, request: OpenAIRequest | Mapping[str, object]) -> AsyncIterator[OpenAIStreamChunk]:
        if not isinstance(request, OpenAIRequest):
            raise TypeError("The model requires a validated request")
        worker = any(tool.function.name == "ask_user" for tool in request.tools or [])
        if worker and not self.check_sent:
            self.check_sent = True
            self.requests.append(request)
            yield OpenAIStreamChunk(
                id="check",
                model=self.model,
                choices=(
                    OpenAIStreamChoice(
                        delta=OpenAIStreamDelta(
                            tool_calls=(
                                OpenAIStreamToolCall(
                                    index=0,
                                    id="unit-tests",
                                    type="function",
                                    function=OpenAIStreamFunction(
                                        name="command", arguments=json.dumps({"command": self.command})
                                    ),
                                ),
                            )
                        ),
                        finish_reason="tool_calls",
                    ),
                ),
                usage=OpenAIUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            )
            return
        async for chunk in super().stream(request):
            yield chunk


class VerificationSandbox(OciSandbox):
    def __init__(self, exit_code: int, *, stdout: bytes = b"Check completed") -> None:
        super().__init__()
        self.exit_code = exit_code
        self.stdout = stdout

    async def execute(
        self, request: SandboxRequest, *, on_accept: Callable[[SandboxReceipt], Awaitable[None]] | None = None
    ) -> SandboxResult:
        """Return the deterministic external process outcome after real host request validation."""
        if on_accept is not None:
            if request.operation_id is None:
                raise ValueError("A background process requires its host operation identity")
            await on_accept(
                SandboxReceipt(
                    runtime="docker",
                    operation_id=request.operation_id,
                    container_name=f"verification-{request.operation_id}",
                    container_id=f"container-{request.operation_id}",
                    admission_token=request.operation_id,
                    request_digest=request.digest(),
                )
            )
        return SandboxResult(exit_code=self.exit_code, backend="verification-process", stdout=self.stdout)


@pytest.mark.parametrize(
    ("executed_command", "exit_code", "expected_status", "check_status"),
    (
        ("python -m pytest", 0, ExecutionStatus.SUCCEEDED, "passed"),
        ("printf success", 0, ExecutionStatus.FAILED, "unknown"),
        ("python -m pytest", 1, ExecutionStatus.FAILED, "failed"),
    ),
    ids=("actual-check-passed", "other-command-cannot-prove-tests", "failing-check-blocks-ready"),
)
def test_required_verification_uses_exact_executed_arguments_and_actual_process_result(
    tmp_path: Path, executed_command: str, exit_code: int, expected_status: ExecutionStatus, check_status: str
) -> None:
    async def scenario() -> None:
        model = VerificationScenarioModel(executed_command)
        service = product_service(tmp_path, model)
        service.agents.sandbox = Sandbox(VerificationSandbox(exit_code))
        space = service.store.create_space(SpaceConfiguration(name="Checks"), request_id="space")
        await service.start()
        try:
            execution = await service.delegate(
                ExecutionSpec(
                    space_id=space.space_id,
                    objective="Run the required unit tests",
                    delivery_contract=DeliveryContract(
                        required_verifications=(
                            ToolVerification(
                                name="unit_tests",
                                tool_code="command",
                                arguments={"command": "python -m pytest"},
                                expected_output={"exit_code": 0, "timed_out": False},
                            ),
                        )
                    ),
                ),
                "work",
            )
            async with asyncio.timeout(5):
                finished = await service.wait(execution.execution_id)
            assert finished.status is expected_status
            reports = service.store.reports(execution.execution_id)
            first = next(value for value in reports if value.kind == "ready")
            checked = next(value for value in first.verification if value.name == "unit_tests")
            assert checked.producer == "host" and checked.status == check_status
            if expected_status is ExecutionStatus.SUCCEEDED:
                assert checked.evidence_refs and checked.executed_at is not None
                assert not first.rejection_reasons
            else:
                assert first.rejection_reasons
                assert not any(value.kind == "ready" and not value.rejection_reasons for value in reports)
        finally:
            await service.shutdown()

    asyncio.run(scenario())


def test_required_artifact_integrity_check_reads_actual_delivered_bytes_and_rejects_corruption(tmp_path: Path) -> None:
    async def scenario() -> None:
        model = ProductScenarioModel()
        service = product_service(tmp_path, model)
        space = service.store.create_space(SpaceConfiguration(name="Checks"), request_id="space")
        execution = service.store.create_execution(
            ExecutionSpec(
                space_id=space.space_id,
                objective="Deliver the brief",
                delivery_contract=DeliveryContract(
                    required_verifications=(ArtifactVerification(name="brief_bytes", artifact_name="brief.txt"),)
                ),
            ),
            request_id="work",
        )
        artifact = service.content.install_artifact(
            execution.execution_id, artifact_id="brief", name="brief.txt", mime="text/plain", payload=b"Original brief"
        )
        journal = await service.sessions.snapshot(execution.session_id)
        verified = await verify_delivery(execution, (artifact.artifact_id,), journal, service.content)
        assert len(verified) == 1 and verified[0].status == "passed" and verified[0].producer == "host"
        assert verified[0].evidence_refs == (artifact.artifact_id,)
        (service.store.content_root / artifact.relative_path).write_bytes(b"Modified brief")

        damaged = await verify_delivery(execution, (artifact.artifact_id,), journal, service.content)

        assert len(damaged) == 1 and damaged[0].status == "failed"
        assert model.requests == []
        await service.agents.aclose()

    asyncio.run(scenario())


class ClarificationScenarioModel(ProductScenarioModel):
    def __init__(self) -> None:
        super().__init__()
        self.question_sent = False
        self.release_workers.set()

    async def stream(self, request: OpenAIRequest | Mapping[str, object]) -> AsyncIterator[OpenAIStreamChunk]:
        if not isinstance(request, OpenAIRequest):
            raise TypeError("The model requires a validated request")
        worker = any(tool.function.name == "ask_user" for tool in request.tools or [])
        if worker and not self.question_sent:
            self.question_sent = True
            self.requests.append(request)
            yield OpenAIStreamChunk(
                id="question",
                model=self.model,
                choices=(
                    OpenAIStreamChoice(
                        delta=OpenAIStreamDelta(
                            tool_calls=(
                                OpenAIStreamToolCall(
                                    index=0,
                                    id="clarify",
                                    type="function",
                                    function=OpenAIStreamFunction(
                                        name="ask_user", arguments='{"question":"Which audience?"}'
                                    ),
                                ),
                            )
                        ),
                        finish_reason="tool_calls",
                    ),
                ),
                usage=OpenAIUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            )
            return
        async for chunk in super().stream(request):
            yield chunk


def test_answer_received_during_manual_pause_resumes_the_original_run_only_after_continue(tmp_path: Path) -> None:
    async def scenario() -> None:
        model = ClarificationScenarioModel()
        service = product_service(tmp_path, model)
        space = service.store.create_space(SpaceConfiguration(name="Research"), request_id="space")
        await service.start()
        try:
            execution = await service.delegate(
                ExecutionSpec(space_id=space.space_id, objective="Write a report"), "work"
            )
            async with asyncio.timeout(5):
                waiting = await service.wait(execution.execution_id)
            assert waiting.status is ExecutionStatus.WAITING_INPUT
            original = waiting.current_run_id
            paused = await service.control(
                execution.execution_id, "pause", request_id="pause", expected_revision=waiting.revision
            )
            assert paused.status is ExecutionStatus.PAUSED
            attention = service.store.attentions().items[0]
            answered = await service.resolve(
                attention.attention_id,
                ResolutionRequest(request_id="answer", expected_revision=attention.revision, answer="Engineering team"),
            )
            assert answered.execution is not None and answered.execution.status is ExecutionStatus.PAUSED
            resumed = await service.control(
                execution.execution_id, "resume", request_id="resume", expected_revision=answered.execution.revision
            )
            assert resumed.status is ExecutionStatus.QUEUED
            async with asyncio.timeout(5):
                completed = await service.wait(execution.execution_id)
            assert completed.status is ExecutionStatus.SUCCEEDED
            assert completed.current_run_id == original
            assert service.store.execution_runs(execution.execution_id)[0].charged_budget_output_tokens == 2
            messages = service.store.messages()
            assert [message.body for message in messages.items if message.role == "user"] == ["Engineering team"]
        finally:
            await service.shutdown()

    asyncio.run(scenario())


def test_worker_materializes_only_assigned_sources_and_publishes_actual_owned_bytes(tmp_path: Path) -> None:
    async def scenario() -> None:
        service = product_service(tmp_path, ProductScenarioModel())
        space = service.store.create_space(SpaceConfiguration(name="Research"), request_id="space")
        execution = service.store.create_execution(
            ExecutionSpec(space_id=space.space_id, objective="Review the attachment"), request_id="work"
        )
        accepted = service.content.upload_attachment(
            client_attachment_id="accepted", name="brief.txt", mime="text/plain", payload=b"Accepted brief"
        )
        foreign = service.content.upload_attachment(
            client_attachment_id="foreign", name="private.txt", mime="text/plain", payload=b"Unrelated document"
        )
        service.store.submit_message(
            ChatMessageRequest(
                client_message_id="assigned",
                text="Use this brief",
                attachment_ids=(accepted.attachment_id,),
                target_execution_ids=(execution.execution_id,),
            )
        )
        binding = await service.agents.worker(execution, service.ignore_event, service.delegate, service.progress)
        tool = binding.runner.tools.load("weave.execution_content")
        context = ToolContext(call_id="source", run_id="run")
        value = await tool.execute(
            context, {"action": "materialize", "kind": "attachment", "id": accepted.attachment_id}
        )
        assert isinstance(value, dict) and isinstance(value["path"], str)
        scratch = service.agents.workspace / "work" / execution.space_id / execution.execution_id
        assert (scratch / value["path"]).read_bytes() == b"Accepted brief"
        with pytest.raises(PermissionError, match="outside"):
            await tool.execute(context, {"action": "materialize", "kind": "attachment", "id": foreign.attachment_id})
        result = await tool.execute(
            ToolContext(call_id="publish", run_id="run"),
            {"action": "publish", "path": value["path"], "name": "result.txt", "mime": "text/plain"},
        )
        assert isinstance(result, dict) and isinstance(result["artifact_id"], str)
        assert result["owner_execution_id"] == execution.execution_id
        assert "relative_path" not in result
        assert service.content.read_artifact(result["artifact_id"]) == b"Accepted brief"
        await binding.runner.aclose()
        await service.agents.aclose()

    asyncio.run(scenario())


def test_artifact_publication_reused_call_id_in_another_run_preserves_both_original_versions(tmp_path: Path) -> None:
    """Repeated tool-call IDs in later SDK Runs create distinct immutable artifacts."""

    async def scenario() -> None:
        service = product_service(tmp_path, ProductScenarioModel())
        space = service.store.create_space(SpaceConfiguration(name="Versioned publication"), request_id="space")
        execution = service.store.create_execution(
            ExecutionSpec(space_id=space.space_id, objective="Publish revisions"), request_id="work"
        )
        binding = await service.agents.worker(execution, service.ignore_event, service.delegate, service.progress)
        tool = binding.runner.tools.load("weave.execution_content")
        scratch = service.agents.workspace / "work" / execution.space_id / execution.execution_id
        path = scratch / "result.txt"
        arguments: JsonObject = {"action": "publish", "path": "result.txt", "name": "result.txt", "mime": "text/plain"}
        outputs: list[str] = []
        for run_id, payload in (("first-run", b"Original bytes"), ("second-run", b"Revised bytes")):
            path.write_bytes(payload)
            context = ToolContext(call_id="reused-call", run_id=run_id)
            output = await tool.execute(context, arguments)
            assert isinstance(output, dict) and isinstance(output["artifact_id"], str)
            assert await tool.execute(context, arguments) == output
            outputs.append(output["artifact_id"])
        assert outputs[0] != outputs[1]
        assert [service.content.read_artifact(identity) for identity in outputs] == [
            b"Original bytes",
            b"Revised bytes",
        ]
        path.write_bytes(b"Changed retry")
        with pytest.raises(ProductConflict):
            await tool.execute(ToolContext(call_id="reused-call", run_id="second-run"), arguments)
        with pytest.raises(ValueError, match="owning SDK Run"):
            await tool.execute(ToolContext(call_id="missing-run"), arguments)
        await binding.runner.aclose()
        await service.agents.aclose()

    asyncio.run(scenario())


def test_service_restart_restores_a_question_and_continues_its_original_sdk_run(tmp_path: Path) -> None:
    async def scenario() -> None:
        first = product_service(tmp_path, ClarificationScenarioModel())
        space = first.store.create_space(SpaceConfiguration(name="Research"), request_id="space")
        await first.start()
        execution = await first.delegate(ExecutionSpec(space_id=space.space_id, objective="Write a report"), "work")
        async with asyncio.timeout(5):
            waiting = await first.wait(execution.execution_id)
        assert waiting.status is ExecutionStatus.WAITING_INPUT
        await first.shutdown()

        model = ClarificationScenarioModel()
        model.question_sent = True
        restored = product_service(tmp_path, model)
        await restored.start()
        try:
            attention = restored.store.attentions().items[0]
            await restored.resolve(
                attention.attention_id,
                ResolutionRequest(request_id="answer", expected_revision=attention.revision, answer="Engineering team"),
            )
            async with asyncio.timeout(5):
                finished = await restored.wait(execution.execution_id)
            assert finished.status is ExecutionStatus.SUCCEEDED
            assert finished.current_run_id == waiting.current_run_id
            assert len(restored.store.execution_runs(execution.execution_id)) == 1
            assert len(model.requests) == 1
        finally:
            await restored.shutdown()

    asyncio.run(scenario())


def test_lost_action_owner_blocks_until_trusted_evidence_then_honors_saved_cancel(tmp_path: Path) -> None:
    async def scenario() -> None:
        model = ProductScenarioModel()
        service = product_service(tmp_path, model)
        space = service.store.create_space(SpaceConfiguration(name="Research"), request_id="space")
        execution = service.store.create_execution(
            ExecutionSpec(space_id=space.space_id, objective="Submit remote work"), request_id="work"
        )
        item = service.store.seed_execution_input(execution.execution_id)
        await service.deliver_inputs()
        session = Session(execution.session_id, service.sessions)
        started = await service.sessions.start(
            session.session_id,
            invocation_id="lost-owner",
            parameters_digest=fingerprint({"owner": "lost"}),
            input_ids=(item.input_id,),
            input_mode="live",
            agent_id="worker",
        )
        writer = SessionWriter(session, started.snapshot)
        step = await writer.begin("worker")
        await writer.append(
            step.step_id,
            (AssistantMessage(tool_calls=(ToolCall(id="remote-call", name="remote_action", arguments="{}"),)),),
        )
        await writer.save_tool_call(
            ToolCallRecord(
                run_id=writer.run_id,
                step_id=step.step_id,
                call_id="remote-call",
                tool_code="remote_action",
                binding_digest="binding",
                arguments_digest="arguments",
                authorization_ref="host",
                state="running",
            )
        )
        service.store.transition_execution(
            execution.execution_id, ExecutionStatus.RUNNING, expected_revision=execution.revision
        )
        service.store.record_execution_run(
            ExecutionRun(
                execution_id=execution.execution_id,
                attempt=1,
                invocation_id="lost-owner",
                run_id=writer.run_id,
                state="running",
            )
        )
        current = service.store.get_execution(execution.execution_id)
        assert current is not None
        service.store.control_execution(
            execution.execution_id, "cancel", request_id="cancel", expected_revision=current.revision
        )
        await service.start()
        try:
            blocked = await service.require_execution(execution.execution_id)
            assert blocked.status is ExecutionStatus.BLOCKED
            assert blocked.control_intent == "cancel" and blocked.unknown_effects == ("remote-call",)
            await service.reconcile_execution(
                blocked,
                evidence=(
                    ActionEvidence(
                        evidence_id="trusted-receipt",
                        action=ActionRef(run_id=writer.run_id, call_id="remote-call"),
                        binding_digest="binding",
                        arguments_digest="arguments",
                        backend_evidence_ref="remote:confirmed",
                        observed_at=datetime.now(UTC),
                        observed_state="succeeded",
                        result_ref="remote:confirmed",
                    ),
                ),
            )
            finished = await service.require_execution(execution.execution_id)
            assert finished.status is ExecutionStatus.CANCELLED
            assert finished.unknown_effects == ()
            calls = await service.sessions.tool_calls(session.session_id, writer.run_id)
            assert len(calls) == 1 and calls[0].state == "succeeded"
            assert len(service.store.execution_runs(execution.execution_id)) == 1
        finally:
            await service.shutdown()

    asyncio.run(scenario())


def test_product_metrics_count_acceptance_requests_without_duplicate_messages(tmp_path: Path) -> None:
    async def scenario() -> None:
        reader = InMemoryMetricReader()
        provider = MeterProvider(metric_readers=[reader])
        service = product_service(
            tmp_path, ProductScenarioModel(), observability=Observability(meter_provider=provider)
        )
        await service.start()
        try:
            request = ChatMessageRequest(client_message_id="one", text="status")
            receipts = [await service.submit(request) for attempt in range(10)]
            assert all(receipt == receipts[0] for receipt in receipts)
            assert [value.body for value in service.store.messages().items if value.role == "user"] == ["status"]
            metrics = reader.get_metrics_data()
            assert metrics is not None
            counts = {
                metric.name: metric.data
                for resource in metrics.resource_metrics
                for scope in resource.scope_metrics
                for metric in scope.metrics
            }
            data = counts["weave.input.acceptance.requests"]
            assert isinstance(data, Sum)
            assert sum(point.value for point in data.data_points) == 10
        finally:
            await service.shutdown()
            provider.shutdown()

    asyncio.run(scenario())


def test_metrics_keep_durable_queued_responsibilities_and_pending_decisions_separate(tmp_path: Path) -> None:
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    service = product_service(tmp_path, ProductScenarioModel(), observability=Observability(meter_provider=provider))
    try:
        space = service.store.create_space(SpaceConfiguration(name="Research"), request_id="space")
        execution = service.store.create_execution(
            ExecutionSpec(space_id=space.space_id, objective="Read the brief", budget_limit=1234), request_id="work"
        )
        request = ChatMessageRequest(
            client_message_id="brief", text="Use this brief", target_execution_ids=(execution.execution_id,)
        )
        service.store.submit_message(request)
        reopened = ProductStore(service.store.root)
        reopened.submit_message(request)
        reopened.create_attention(
            Attention(attention_id="clarify", execution_id=None, kind="clarification", question="Which audience?")
        )
        measured = reader.get_metrics_data()
        assert measured is not None
        metrics = {
            metric.name: metric.data
            for resource in measured.resource_metrics
            for scope in resource.scope_metrics
            for metric in scope.metrics
        }
        queued = metrics["weave.inbox.pending"]
        assert isinstance(queued, Gauge)
        assert sum(point.value for point in queued.data_points) == 2
        responsibilities = metrics["weave.input.responsibilities"]
        assert isinstance(responsibilities, Gauge)
        assert {
            str(point.attributes["state"]): point.value for point in responsibilities.data_points if point.attributes
        } == {
            "owned": 1,
            "handled": 0,
            "returned": 0,
        }
        decisions = metrics["weave.attention.pending"]
        assert isinstance(decisions, Gauge)
        assert sum(point.value for point in decisions.data_points) == 1
        age = metrics["weave.attention.oldest_age"]
        assert isinstance(age, Gauge) and age.data_points[0].value >= 0
        budgets = metrics["weave.execution.budget"]
        assert isinstance(budgets, Gauge)
        assert {str(point.attributes["phase"]): point.value for point in budgets.data_points if point.attributes} == {
            "limit": 1234,
            "consumed": 0,
            "reserved": 0,
        }
        events = metrics["weave.product.events"]
        assert isinstance(events, Sum)
        event_counts = {str(point.attributes["type"]): point.value for point in events.data_points if point.attributes}
        assert event_counts["chat.message.created"] == 1
        assert event_counts["attention.created"] == 1
    finally:
        provider.shutdown()


def test_cancellation_cleanup_duration_waits_for_real_child_settlement_and_records_each_owner_once(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        reader = InMemoryMetricReader()
        provider = MeterProvider(metric_readers=[reader])
        service = product_service(
            tmp_path, ProductScenarioModel(), observability=Observability(meter_provider=provider)
        )
        space = service.store.create_space(SpaceConfiguration(name="Checks"), request_id="space")
        parent = service.store.create_execution(
            ExecutionSpec(space_id=space.space_id, objective="Parent"), request_id="parent"
        )
        child = service.store.create_execution(
            ExecutionSpec(
                space_id=space.space_id, objective="Child", parent_execution_id=parent.execution_id, budget_limit=100
            ),
            request_id="child",
        )
        current = service.store.get_execution(parent.execution_id)
        assert current is not None
        service.store.control_execution(
            parent.execution_id, "cancel", request_id="cancel", expected_revision=current.revision
        )
        try:
            await service.finish_cancellation(parent.execution_id)
            assert service.store.cancellation_elapsed(parent.execution_id) is None
            await service.finish_cancellation(child.execution_id)
            await service.finish_cancellation(parent.execution_id)
            await service.finish_cancellation(parent.execution_id)
            measured = reader.get_metrics_data()
            assert measured is not None
            duration = next(
                metric.data
                for resource in measured.resource_metrics
                for scope in resource.scope_metrics
                for metric in scope.metrics
                if metric.name == "weave.cancellation.cleanup.duration"
            )
            assert isinstance(duration, Histogram)
            assert {
                str(point.attributes["scope"]): point.count for point in duration.data_points if point.attributes
            } == {"root": 1, "child": 1}
            assert all(point.sum >= 0 for point in duration.data_points)
            completed = service.store.get_execution(parent.execution_id)
            assert completed is not None and completed.status is ExecutionStatus.CANCELLED
        finally:
            await service.agents.aclose()
            provider.shutdown()

    asyncio.run(scenario())


def test_worker_files_are_isolated_and_shared_data_requires_an_explicit_resource_binding(tmp_path: Path) -> None:
    async def scenario() -> None:
        service = product_service(tmp_path, ProductScenarioModel())
        workspace = service.agents.workspace
        (workspace / "shared.txt").write_text("Shared reference", encoding="utf-8")
        authorized = service.store.create_space(
            SpaceConfiguration(name="Authorized", resource_bindings=("workspace",)), request_id="authorized"
        )
        isolated = service.store.create_space(SpaceConfiguration(name="Isolated"), request_id="isolated")
        execution = service.store.create_execution(
            ExecutionSpec(space_id=authorized.space_id, objective="Read references"), request_id="one"
        )
        other = service.store.create_execution(
            ExecutionSpec(space_id=isolated.space_id, objective="Independent work"), request_id="two"
        )
        binding = await service.agents.worker(execution, service.ignore_event, service.delegate, service.progress)
        second = await service.agents.worker(other, service.ignore_event, service.delegate, service.progress)
        try:
            resource = binding.runner.tools.load("weave.workspace_resource")
            listing = await resource.execute(ToolContext(call_id="list"), {})
            assert isinstance(listing, dict) and isinstance(listing["items"], list)
            assert all(
                isinstance(value, dict) and value["name"] not in {"runtime-v2", "work"} for value in listing["items"]
            )
            materialized = await resource.execute(
                ToolContext(call_id="source"), {"action": "materialize", "path": "shared.txt"}
            )
            assert isinstance(materialized, dict) and isinstance(materialized["path"], str)
            root = workspace / "work" / execution.space_id / execution.execution_id
            assert (root / materialized["path"]).read_bytes() == b"Shared reference"
            with pytest.raises(ToolFileAccessDeniedError):
                await resource.execute(
                    ToolContext(call_id="private"), {"action": "materialize", "path": "runtime-v2/product.sqlite3"}
                )
            with pytest.raises(LookupError):
                second.runner.tools.load("weave.workspace_resource")
            writer = binding.runner.tools.load("write_file")
            await writer.execute(ToolContext(call_id="write"), {"path": "own.txt", "content": "Owned output"})
            assert (root / "own.txt").read_text(encoding="utf-8") == "Owned output"
            with pytest.raises(ToolFileNotFoundError):
                await second.runner.tools.load("read_file").execute(
                    ToolContext(call_id="foreign"), {"scope": "workspace", "path": "own.txt"}
                )
        finally:
            await binding.runner.aclose()
            await second.runner.aclose()
            await service.agents.aclose()

    asyncio.run(scenario())


def test_memory_cli_edits_are_recalled_by_the_worker_from_the_same_runtime_root(tmp_path: Path) -> None:
    async def scenario() -> None:
        service = product_service(tmp_path, ProductScenarioModel())
        facts = FactStore(service.store.root, user_id="default-user")
        service.agents.facts = facts
        space = service.store.create_space(SpaceConfiguration(name="Preferences"), request_id="space")
        facts.save_fact(MemoryFact(memory_id="drink", scope="space", scope_id=space.space_id, content="favorite tea"))
        updated = CliRunner().invoke(
            memory_app,
            [
                "--workspace",
                str(service.agents.workspace),
                "--space-id",
                space.space_id,
                "update",
                "drink",
                "--content",
                "favorite coffee",
                "--expected-revision",
                "1",
            ],
        )
        assert updated.exit_code == 0, updated.output
        execution = service.store.create_execution(
            ExecutionSpec(space_id=space.space_id, objective="Use the favorite drink"), request_id="work"
        )
        binding = await service.agents.worker(execution, service.ignore_event, service.delegate, service.progress)
        try:
            assert [value.content for value in binding.context if value.kind == "product_fact"] == ["favorite coffee"]
        finally:
            await binding.runner.aclose()
            await service.agents.aclose()

    asyncio.run(scenario())


def test_router_missing_source_is_a_recoverable_tool_observation(tmp_path: Path) -> None:
    async def scenario() -> None:
        model = ProductScenarioModel()
        service = product_service(tmp_path, model)
        receipt = service.store.submit_message(ChatMessageRequest(client_message_id="status", text="status"))
        attempt = service.store.claim_router_batch()
        assert attempt is not None
        binding = await service.agents.router(attempt, service.ignore_event)
        try:
            tool = binding.runner.tools.load("weave.query_product")
            result = await binding.runner.tool_runtime.execute(
                tool,
                RuntimeToolCall(
                    call_id="query",
                    tool_code="weave.query_product",
                    arguments={"kind": "report", "id": "missing-report"},
                ),
                ToolContext(call_id="query"),
            )
            assert isinstance(result, ToolResult) and not result.ok and result.error_code == "weave.source_not_found"
            message = await tool.execute(ToolContext(call_id="message"), {"kind": "message", "id": receipt.message_id})
            assert isinstance(message, dict) and message["body"] == "status"
            assert model.requests == []
        finally:
            await binding.runner.aclose()
            await service.agents.aclose()

    asyncio.run(scenario())


def test_delegation_during_parent_model_admission_keeps_the_intent_and_provisional_budget_unspent(
    tmp_path: Path,
) -> None:
    """A host cannot turn an in-flight model reserve into permanent product spending."""

    async def scenario() -> None:
        entered, release = asyncio.Event(), asyncio.Event()

        class ParentDelegationModel(ProductScenarioModel):
            async def stream(self, request: OpenAIRequest | Mapping[str, object]) -> AsyncIterator[OpenAIStreamChunk]:
                assert isinstance(request, OpenAIRequest)
                worker = any(tool.function.name == "ask_user" for tool in request.tools or [])
                if (
                    worker
                    and "Parent grant boundary" in request.model_dump_json()
                    and not any(message.role == "tool" for message in request.messages)
                ):
                    entered.set()
                    await release.wait()
                    yield OpenAIStreamChunk(
                        model=self.model,
                        choices=(
                            OpenAIStreamChoice(
                                delta=OpenAIStreamDelta(
                                    tool_calls=(
                                        OpenAIStreamToolCall(
                                            index=0,
                                            id="delegate-call",
                                            function=OpenAIStreamFunction(
                                                name="delegate_execution",
                                                arguments=json.dumps(
                                                    {
                                                        "space_id": space.space_id,
                                                        "objective": "Child",
                                                        "budget_limit": 10,
                                                    }
                                                ),
                                            ),
                                        ),
                                    )
                                ),
                                finish_reason="tool_calls",
                            ),
                        ),
                        usage=OpenAIUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
                    )
                    return
                async for chunk in super().stream(request):
                    yield chunk

        model = ParentDelegationModel()
        service = product_service(tmp_path, model)
        space = service.store.create_space(SpaceConfiguration(name="Admission"), request_id="space")
        await service.start()
        try:
            parent = await service.delegate(
                ExecutionSpec(space_id=space.space_id, objective="Parent grant boundary"), "parent"
            )
            async with asyncio.timeout(5):
                await entered.wait()
            spec = ExecutionSpec(
                space_id=space.space_id, parent_execution_id=parent.execution_id, objective="Child", budget_limit=10
            )
            current = await service.require_execution(parent.execution_id)
            assert current.current_run_id is not None
            request_id = f"delegate:{current.current_run_id}:delegate-call"
            with pytest.raises(ProductConflict, match="admitted model request"):
                await service.delegate(spec, request_id)
            current = await service.require_execution(parent.execution_id)
            assert current.budget_consumed == current.budget_reserved == 0
            assert service.store.list_executions().total == 1
            release.set()
            async with asyncio.timeout(5):
                async for event in service.events():
                    current = await service.require_execution(parent.execution_id)
                    if event.subject == parent.execution_id and current.status is ExecutionStatus.WAITING_CHILDREN:
                        break
            waiting = await service.require_execution(parent.execution_id)
            assert waiting.status is ExecutionStatus.WAITING_CHILDREN
            child = await service.delegate(spec, request_id)
            assert child.parent_execution_id == parent.execution_id
            assert service.store.list_executions().total == 2
            current = await service.require_execution(parent.execution_id)
            assert current.budget_consumed == 1 and current.budget_reserved == 10
            service.store.update_space(
                space.space_id,
                SpaceConfiguration(name="Changed", model_role="unavailable"),
                request_id="change",
                expected_revision=space.revision,
            )
            retried = await service.delegate(spec, request_id)
            assert retried == child
            assert service.store.list_executions().total == 2
        finally:
            release.set()
            model.release_workers.set()
            await service.shutdown()

    asyncio.run(scenario())


def test_retry_validates_the_current_space_and_returns_its_committed_result_after_later_changes(tmp_path: Path) -> None:
    async def scenario() -> None:
        model = ProductScenarioModel()
        service = product_service(tmp_path, model)
        original = service.store.create_space(
            SpaceConfiguration(name="Original", model_role="text"), request_id="space"
        )
        source = service.store.create_execution(
            ExecutionSpec(space_id=original.space_id, objective="Original objective"), request_id="source"
        )
        source = service.store.transition_execution(
            source.execution_id, ExecutionStatus.FAILED, expected_revision=source.revision
        )
        current = service.store.update_space(
            original.space_id,
            SpaceConfiguration(name="Current", model_role="current"),
            request_id="current",
            expected_revision=original.revision,
        )
        service.agents.models = {"current": model}
        created = await service.retry(source.execution_id, request_id="retry", expected_revision=source.revision)
        assert created.config_snapshot == current
        assert created.source_execution_id == source.execution_id
        assert source.config_snapshot == original
        service.store.update_space(
            current.space_id,
            SpaceConfiguration(name="Withdrawn", model_role="missing"),
            request_id="withdrawn",
            expected_revision=current.revision,
        )
        assert (
            await service.retry(source.execution_id, request_id="retry", expected_revision=source.revision) == created
        )
        assert service.store.list_executions().total == 2
        assert len(service.store.worker_inputs(created.execution_id, pending_only=False)) == 1
        assert model.requests == []

    asyncio.run(scenario())


def test_retry_rejects_an_unavailable_current_space_before_creating_any_work(tmp_path: Path) -> None:
    async def scenario() -> None:
        model = ProductScenarioModel()
        service = product_service(tmp_path, model)
        original = service.store.create_space(SpaceConfiguration(name="Original"), request_id="space")
        source = service.store.create_execution(
            ExecutionSpec(space_id=original.space_id, objective="Original objective"), request_id="source"
        )
        source = service.store.transition_execution(
            source.execution_id, ExecutionStatus.FAILED, expected_revision=source.revision
        )
        service.store.update_space(
            original.space_id,
            SpaceConfiguration(name="Unavailable", model_role="missing"),
            request_id="withdrawn",
            expected_revision=original.revision,
        )
        with pytest.raises(ValueError, match="Model role is unavailable: missing"):
            await service.retry(source.execution_id, request_id="retry", expected_revision=source.revision)
        assert service.store.list_executions().total == 1
        assert service.store.messages().total == 0
        assert service.store.get_execution(source.execution_id) == source
        assert model.requests == []

    asyncio.run(scenario())


class LargeSourceModel(ProductScenarioModel):
    def __init__(self) -> None:
        super().__init__()
        self.expected: dict[str, str] = {}
        self.pages: dict[str, dict[int, str]] = {}
        self.finished_inputs: set[str] = set()

    async def stream(self, request: OpenAIRequest | Mapping[str, object]) -> AsyncIterator[OpenAIStreamChunk]:
        if not isinstance(request, OpenAIRequest):
            raise TypeError("The provider requires its validated request")
        self.requests.append(request)
        texts: list[str] = []
        for message in request.messages:
            if isinstance(message.content, str):
                texts.append(message.content)
            elif isinstance(message.content, list):
                for part in message.content:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        text = part["text"]
                        if isinstance(text, str):
                            texts.append(text)
        joined = "\n".join(texts)
        context = JSON_OBJECT.validate_python(json.JSONDecoder().raw_decode(joined[joined.index('{"inputs":') :])[0])
        input_ids = TypeAdapter[tuple[str, ...]](tuple[str, ...]).validate_python(context["source_input_ids"])
        for message in request.messages:
            if message.role != "tool" or not isinstance(message.content, str):
                continue
            result = JSON_OBJECT.validate_json(message.content)
            output = result.get("output")
            if not isinstance(output, dict):
                continue
            reference = output.get("reference")
            text = output.get("content")
            offset = output.get("start_char")
            if not isinstance(reference, dict) or not isinstance(text, str) or not isinstance(offset, int):
                continue
            parsed = ResultReference.model_validate(reference)
            identity = parsed.reference.removeprefix("weave:input:")
            if identity not in input_ids or identity not in self.expected:
                continue
            expected = self.expected[identity]
            assert parsed.digest == hashlib.sha256(expected.encode("utf-8")).hexdigest()
            assert text
            assert text == expected[offset : offset + len(text)]
            self.pages.setdefault(identity, {})[offset] = text
        calls: list[OpenAIStreamToolCall] = []
        for identity in input_ids:
            original = self.expected.get(identity)
            if original is None:
                continue
            received = self.pages.setdefault(identity, {})
            start = 0
            while start in received:
                start += len(received[start])
            if start == len(original):
                assert "".join(received[offset] for offset in sorted(received)) == original
                self.finished_inputs.add(identity)
                continue
            calls.append(
                OpenAIStreamToolCall(
                    index=len(calls),
                    id=f"read-{identity}-{start}",
                    type="function",
                    function=OpenAIStreamFunction(
                        name="query_product",
                        arguments=json.dumps(
                            {
                                "kind": "input",
                                "id": identity,
                                "start_char": start,
                                "max_chars": 32768,
                                "expected_digest": hashlib.sha256(original.encode("utf-8")).hexdigest(),
                            }
                        ),
                    ),
                )
            )
        content = None if calls else json.dumps({"reply": "The original inputs were read.", "actions": []})
        yield OpenAIStreamChunk(
            id=f"large-source-{len(self.requests)}",
            model=self.model,
            choices=(
                OpenAIStreamChoice(
                    delta=OpenAIStreamDelta(tool_calls=tuple(calls), content=content),
                    finish_reason="tool_calls" if calls else "stop",
                ),
            ),
            usage=OpenAIUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        )


def test_twenty_four_maximum_size_inputs_are_read_by_reference_and_do_not_poison_a_later_status_query(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        model = LargeSourceModel()
        service = product_service(tmp_path, model)
        space = service.store.create_space(
            SpaceConfiguration(name="Long instructions", instructions="preserved instructions " * 4_000),
            request_id="space",
        )
        for index in range(24):
            prefix = f"Original source {index}: "
            text = prefix + "x" * (65_536 - len(prefix))
            receipt = service.store.submit_message(ChatMessageRequest(client_message_id=f"large-{index}", text=text))
            model.expected[receipt.input_ids[0]] = text
        batch = service.store.claim_router_batch()
        assert batch is not None and len(batch.source_input_ids) == 2
        assert (
            sum(len(model.expected[identity].encode("utf-8")) for identity in batch.source_input_ids)
            == ROUTER_BATCH_SOURCE_BYTES
        )
        binding = await service.agents.router(batch, service.ignore_event)
        assert len(binding.context[0].content.encode("utf-8")) < 200_000
        projected = JSON_OBJECT.validate_json(binding.context[0].content)
        snapshot = projected["snapshot"]
        assert isinstance(snapshot, dict)
        spaces = snapshot["spaces"]
        assert isinstance(spaces, dict) and isinstance(spaces["items"], list)
        reference_record = spaces["items"][0]
        assert isinstance(reference_record, dict)
        reference = ResultReference.model_validate(reference_record["reference"])
        query = binding.runner.tools.load("weave.query_product")
        read = await binding.runner.tool_runtime.execute(
            query,
            RuntimeToolCall(
                call_id="space-page",
                tool_code="weave.query_product",
                arguments={
                    "kind": "space",
                    "id": space.space_id,
                    "expected_digest": reference.digest,
                    "max_chars": 32768,
                },
            ),
            ToolContext(call_id="space-page"),
        )
        assert isinstance(read, ToolResult) and read.ok and read.output is not None
        assert read.output["content"] == space.model_dump_json()[:32768]
        await service.start()
        try:
            async with asyncio.timeout(60):
                async for event in service.events():
                    if event.type == "chat.message.created" and len(model.finished_inputs) == 24:
                        all_inputs = [service.store.get_input(identity) for identity in model.expected]
                        if all(item is not None and item.status == "handled" for item in all_inputs):
                            break
            assert model.finished_inputs == set(model.expected)
            assert service.store.messages(limit=100).total == 36
            attempts = await service.all_records("route_attempts", RouteAttempt)
            assert len(attempts) == 12 and all(value.state == "applied" and value.attempt == 1 for value in attempts)
            status = await service.submit(ChatMessageRequest(client_message_id="status-after-large", text="status"))
            async with asyncio.timeout(5):
                async for event in service.events():
                    if event.type == "chat.message.created" and event.data.get("role") == "assistant":
                        messages = service.store.messages(limit=100).items
                        if any(status.message_id in value.source_message_ids for value in messages):
                            break
            assert service.ready and not service.store.list_executions().items
            for identity, expected in model.expected.items():
                stored = service.store.get_input(identity)
                assert stored is not None and stored.content == expected
            journal = await service.sessions.snapshot("router")
            assert all(
                isinstance(item.envelope.content, ResultReference)
                for item in journal.inputs
                if item.envelope.input_id in model.expected
            )
            assert all(value.receipt is not None and value.receipt.state == "succeeded" for value in journal.runs)
        finally:
            await service.shutdown()
            await binding.runner.aclose()

    asyncio.run(scenario())


def test_full_worker_input_projection_defers_delivery_without_starving_another_owned_session(tmp_path: Path) -> None:
    class InputPressureModel(ProductScenarioModel):
        def __init__(self) -> None:
            super().__init__()
            self.entered = asyncio.Event()

        async def stream(self, request: OpenAIRequest | Mapping[str, object]) -> AsyncIterator[OpenAIStreamChunk]:
            if isinstance(request, OpenAIRequest) and any(
                tool.function.name == "ask_user" for tool in request.tools or []
            ):
                self.entered.set()
            async for chunk in super().stream(request):
                yield chunk

    async def scenario() -> None:
        model = InputPressureModel()
        service = product_service(tmp_path, model)
        service.max_workers = 1
        space = service.store.create_space(SpaceConfiguration(name="Input pressure"), request_id="space")
        await service.start()
        try:
            first = await service.delegate(
                ExecutionSpec(space_id=space.space_id, objective="Read the supplements"), "first"
            )
            async with asyncio.timeout(5):
                await model.entered.wait()
            originals: dict[str, str] = {}
            for index in range(160):
                text = f"Supplement {index} " + "data" * 600
                receipt = service.store.submit_message(
                    ChatMessageRequest(
                        client_message_id=f"supplement-{index}", text=text, target_execution_ids=(first.execution_id,)
                    )
                )
                originals[receipt.input_ids[-1]] = text
            second = await service.delegate(
                ExecutionSpec(space_id=space.space_id, objective="Another owned objective"), "second"
            )
            async with asyncio.TaskGroup() as group:
                group.create_task(service.deliver_inputs())
                group.create_task(service.deliver_inputs())
            first_journal = await service.sessions.snapshot(first.session_id)
            pending = [value for value in first_journal.inputs if value.step_id is None]
            assert 0 < len(pending) < len(originals)
            assert (
                sum(len(value.envelope.message().model_dump_json().encode("utf-8")) for value in pending)
                <= MODEL_INPUT_QUEUE_BYTES
            )
            assert any(value.input_id in originals for value in service.store.pending_deliveries())
            second_pending = await Session(second.session_id, service.sessions).pending_inputs()
            assert len(second_pending) == 1
            assert (await service.require_execution(second.execution_id)).status is ExecutionStatus.QUEUED
            for value in pending:
                assert isinstance(value.envelope.content, ResultReference)
            own_input = pending[0]
            assert isinstance(own_input.envelope.content, ResultReference)
            worker = service.bindings[first.execution_id]
            query = worker.runner.tools.load("weave.query_work")
            source = await worker.runner.tool_runtime.execute(
                query,
                RuntimeToolCall(
                    call_id="own-input",
                    tool_code="weave.query_work",
                    arguments={
                        "kind": "input",
                        "id": own_input.envelope.input_id,
                        "expected_digest": own_input.envelope.content.digest,
                    },
                ),
                ToolContext(call_id="own-input"),
            )
            assert isinstance(source, ToolResult) and source.ok and source.output is not None
            assert source.output["content"] == originals[own_input.envelope.input_id]
            denied = await worker.runner.tool_runtime.execute(
                query,
                RuntimeToolCall(
                    call_id="foreign-input",
                    tool_code="weave.query_work",
                    arguments={"kind": "input", "id": second_pending[0].envelope.input_id},
                ),
                ToolContext(call_id="foreign-input"),
            )
            assert isinstance(denied, ToolResult) and not denied.ok and denied.error_code == "weave.source_not_found"
            changed = await worker.runner.tool_runtime.execute(
                query,
                RuntimeToolCall(
                    call_id="changed-input",
                    tool_code="weave.query_work",
                    arguments={
                        "kind": "input",
                        "id": own_input.envelope.input_id,
                        "expected_digest": hashlib.sha256(b"different").hexdigest(),
                    },
                ),
                ToolContext(call_id="changed-input"),
            )
            assert isinstance(changed, ToolResult) and not changed.ok and changed.error_code == "weave.source_changed"
            model.release_workers.set()
            async with asyncio.timeout(15):
                async for event in service.events():
                    if event.data.get("status") == "succeeded":
                        states = [
                            (await service.require_execution(identity)).status
                            for identity in (first.execution_id, second.execution_id)
                        ]
                        if states == [ExecutionStatus.SUCCEEDED, ExecutionStatus.SUCCEEDED]:
                            break
            assert len(service.store.execution_runs(first.execution_id)) == 1
            first_inputs = service.store.worker_inputs(first.execution_id, pending_only=False)
            assert len(first_inputs) == len(originals) + 1 and all(value.status == "handled" for value in first_inputs)
            for identity, original in originals.items():
                actual = service.store.get_input(identity)
                assert actual is not None and actual.content == original
            assert service.ready
        finally:
            model.release_workers.set()
            await service.shutdown()

    asyncio.run(scenario())
