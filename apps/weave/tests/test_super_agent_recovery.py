import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

import httpx
import pytest
from openai import AsyncOpenAI, InternalServerError
from pleiad.messages.message import AssistantMessage, ToolCall, ToolMessage
from pleiad.models.errors import ModelProviderOverloadedError
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
from pleiad.models.providers import deepseek as deepseek_provider
from pleiad.runtime.limits import RunLimits
from pleiad.runtime.result import RunSuspension
from pleiad.sandbox.backends.oci import OciSandbox, create_container_identity
from pleiad.sandbox.backends.process import ProcessResult
from pleiad.sandbox.models import SandboxReceipt, SandboxRequest, SandboxResult
from pleiad.sandbox.sandbox import Sandbox
from pleiad.sessions.checkpoint import RunCheckpoint
from pleiad.sessions.input import InputEnvelope
from pleiad.sessions.records import OperationRecord, RunReceipt, ToolCallRecord
from pleiad.sessions.recovery import ActionEvidence, ActionRef
from pleiad.sessions.session import Session
from pleiad.sessions.writer import SessionWriter
from pleiad.tools.contracts import ToolAcceptance, ToolSubmission

from pleiad_weave.application.service import ExecutionService
from pleiad_weave.infrastructure.storage.memory import FactStore, MemoryFact
from pleiad_weave.infrastructure.storage.product import JSON_OBJECT, fingerprint
from pleiad_weave.infrastructure.storage.product_details import ToolResultDetail
from pleiad_weave.infrastructure.storage.product_models import (
    ChatMessageRequest,
    ExecutionRun,
    ExecutionSpec,
    ExecutionStatus,
    Handback,
    HandbackResolution,
    InboxItem,
    InputDisposition,
    ProductCapacityError,
    ProductConflict,
    ResolutionRequest,
    RouteAttempt,
    SpaceConfiguration,
)
from tests.test_super_agent_application import ClarificationScenarioModel, ProductScenarioModel, product_service


@pytest.mark.parametrize("published_mapping", [False, True])
def test_restart_projects_a_committed_sdk_receipt_without_starting_another_worker(
    tmp_path: Path, published_mapping: bool
) -> None:
    async def scenario() -> None:
        model = ProductScenarioModel()
        model.release_workers.set()
        service = product_service(tmp_path, model)
        space = service.store.create_space(SpaceConfiguration(name="Research"), request_id="space")
        execution = service.store.create_execution(
            ExecutionSpec(space_id=space.space_id, objective="Complete accepted work"), request_id="work"
        )
        item = service.store.seed_execution_input(execution.execution_id)
        await service.deliver_inputs()
        service.store.transition_execution(
            execution.execution_id, ExecutionStatus.RUNNING, expected_revision=execution.revision
        )
        mapping = ExecutionRun(execution_id=execution.execution_id, attempt=1, invocation_id="accepted/1")
        service.store.record_execution_run(mapping)
        binding = await service.agents.worker(execution, service.ignore_event, service.delegate, service.progress)
        binding.ready.set()
        handle = await binding.runner.start(
            binding.agent,
            session=Session(execution.session_id, service.sessions),
            invocation_id=mapping.invocation_id,
            input_ids=(item.input_id,),
            input_mode="live",
            limits=RunLimits(execution_timeout_seconds=1800, max_model_steps=64, max_output_tokens=100_000),
            context=binding.context,
        )
        outcome = await handle.result()
        assert outcome.state.value == "succeeded"
        if published_mapping:
            service.store.record_execution_run(mapping.model_copy(update={"run_id": handle.run_id, "state": "running"}))
        await binding.runner.aclose()
        requests = len(model.requests)

        await service.start()
        try:
            finished = await service.require_execution(execution.execution_id)
            assert finished.status is ExecutionStatus.SUCCEEDED
            assert finished.current_run_id == handle.run_id
            assert len(model.requests) == requests
            runs = service.store.execution_runs(execution.execution_id)
            assert len(runs) == 1 and runs[0].state == "succeeded"
            dispositions = await service.all_records("input_dispositions", InputDisposition)
            assert len(dispositions) == 1 and dispositions[0].state == "handled"
        finally:
            await service.shutdown()

    asyncio.run(scenario())


def test_restart_projects_an_unpublished_safe_checkpoint_and_resumes_the_original_invocation(tmp_path: Path) -> None:
    async def scenario() -> None:
        model = ClarificationScenarioModel()
        service = product_service(tmp_path, model)
        space = service.store.create_space(SpaceConfiguration(name="Research"), request_id="space")
        execution = service.store.create_execution(
            ExecutionSpec(space_id=space.space_id, objective="Write a report"), request_id="work"
        )
        item = service.store.seed_execution_input(execution.execution_id)
        await service.deliver_inputs()
        service.store.transition_execution(
            execution.execution_id, ExecutionStatus.RUNNING, expected_revision=execution.revision
        )
        mapping = ExecutionRun(execution_id=execution.execution_id, attempt=1, invocation_id="accepted/1")
        service.store.record_execution_run(mapping)
        binding = await service.agents.worker(execution, service.ignore_event, service.delegate, service.progress)
        binding.ready.set()
        handle = await binding.runner.start(
            binding.agent,
            session=Session(execution.session_id, service.sessions),
            invocation_id=mapping.invocation_id,
            input_ids=(item.input_id,),
            input_mode="live",
            limits=RunLimits(execution_timeout_seconds=1800, max_model_steps=64, max_output_tokens=100_000),
            context=binding.context,
        )
        paused = await handle.result()
        assert isinstance(paused, RunSuspension)
        await binding.runner.aclose()

        await service.start()
        try:
            waiting = await service.require_execution(execution.execution_id)
            assert waiting.status is ExecutionStatus.WAITING_INPUT
            assert waiting.current_run_id == handle.run_id
            mapping = service.store.execution_runs(execution.execution_id)[0]
            assert mapping.state == "paused" and mapping.checkpoint_ref == handle.run_id
            attention = service.store.attentions().items[0]
            await service.resolve(
                attention.attention_id,
                ResolutionRequest(request_id="answer", expected_revision=attention.revision, answer="Engineering"),
            )
            async with asyncio.timeout(5):
                finished = await service.wait(execution.execution_id)
            assert finished.status is ExecutionStatus.SUCCEEDED
            assert finished.current_run_id == handle.run_id
            assert len(service.store.execution_runs(execution.execution_id)) == 1
        finally:
            await service.shutdown()

    asyncio.run(scenario())


def test_restart_reuses_an_invocation_intent_that_was_never_accepted_by_the_sdk(tmp_path: Path) -> None:
    async def scenario() -> None:
        model = ProductScenarioModel()
        model.release_workers.set()
        service = product_service(tmp_path, model)
        space = service.store.create_space(SpaceConfiguration(name="Research"), request_id="space")
        execution = service.store.create_execution(
            ExecutionSpec(space_id=space.space_id, objective="Complete accepted work"), request_id="work"
        )
        service.store.transition_execution(
            execution.execution_id, ExecutionStatus.RUNNING, expected_revision=execution.revision
        )
        service.store.record_execution_run(
            ExecutionRun(execution_id=execution.execution_id, attempt=1, invocation_id="accepted/1")
        )
        await service.start()
        try:
            async with asyncio.timeout(5):
                finished = await service.wait(execution.execution_id)
            assert finished.status is ExecutionStatus.SUCCEEDED
            runs = service.store.execution_runs(execution.execution_id)
            assert len(runs) == 1 and runs[0].invocation_id == "accepted/1"
            assert len(model.requests) == 1
        finally:
            await service.shutdown()

    asyncio.run(scenario())


def test_revoked_model_resource_blocks_restart_but_still_allows_confirmed_cancellation(tmp_path: Path) -> None:
    async def scenario() -> None:
        original_model = ClarificationScenarioModel()
        service = product_service(tmp_path, original_model)
        service.agents.models = {"text": original_model, "revoked": original_model}
        space = service.store.create_space(
            SpaceConfiguration(name="Research", model_role="revoked"), request_id="space"
        )
        await service.start()
        execution = await service.delegate(ExecutionSpec(space_id=space.space_id, objective="Write a report"), "work")
        async with asyncio.timeout(5):
            waiting = await service.wait(execution.execution_id)
        assert waiting.status is ExecutionStatus.WAITING_INPUT
        await service.shutdown()

        model = ProductScenarioModel()
        restored = product_service(tmp_path, model)
        await restored.start()
        try:
            blocked = await restored.require_execution(execution.execution_id)
            assert restored.ready and blocked.status is ExecutionStatus.BLOCKED
            assert blocked.reason == "resource_unavailable"
            await restored.control(
                execution.execution_id, "cancel", request_id="cancel", expected_revision=blocked.revision
            )
            async with asyncio.timeout(5):
                finished = await restored.wait(execution.execution_id)
            assert finished.status is ExecutionStatus.CANCELLED
            assert finished.current_run_id == waiting.current_run_id
            assert finished.current_run_id is not None
            snapshot = await restored.sessions.run(finished.session_id, finished.current_run_id)
            assert snapshot.receipt is not None and snapshot.receipt.state == "cancelled"
            assert all(
                any(tool.function.name == "query_product" for tool in request.tools or []) for request in model.requests
            )
            attentions = tuple(
                value
                for value in restored.store.attentions(status=None).items
                if value.execution_id == execution.execution_id
            )
            assert attentions and all(value.status == "withdrawn" for value in attentions)
        finally:
            await restored.shutdown()

    asyncio.run(scenario())


class NestedApprovalModel(ProductScenarioModel):
    def __init__(self) -> None:
        super().__init__()
        self.consulted = False
        self.specialist_started = False
        self.release_workers.set()

    async def stream(self, request: OpenAIRequest | Mapping[str, object]) -> AsyncIterator[OpenAIStreamChunk]:
        if not isinstance(request, OpenAIRequest):
            raise TypeError("The provider requires a validated protocol request")
        names = {tool.function.name for tool in request.tools or []}
        calls: tuple[OpenAIStreamToolCall, ...] = ()
        text = None
        if "consult_specialist" in names:
            if self.consulted:
                async for chunk in super().stream(request):
                    yield chunk
                return
            self.consulted = True
            calls = (
                OpenAIStreamToolCall(
                    index=0,
                    id="consult",
                    type="function",
                    function=OpenAIStreamFunction(name="consult_specialist", arguments='{"task":"Write both notes"}'),
                ),
            )
        elif "query_product" in names:
            async for chunk in super().stream(request):
                yield chunk
            return
        elif not self.specialist_started:
            self.specialist_started = True
            calls = tuple(
                OpenAIStreamToolCall(
                    index=index,
                    id=f"write-{name}",
                    type="function",
                    function=OpenAIStreamFunction(
                        name="write_file", arguments=json.dumps({"path": f"{name}.txt", "content": name})
                    ),
                )
                for index, name in enumerate(("first", "second"))
            )
        else:
            text = "Both approved notes were written."
        self.requests.append(request)
        yield OpenAIStreamChunk(
            id=f"response-{len(self.requests)}",
            model=self.model,
            choices=(
                OpenAIStreamChoice(
                    delta=OpenAIStreamDelta(content=text, tool_calls=calls),
                    finish_reason="tool_calls" if calls else "stop",
                ),
            ),
            usage=OpenAIUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        )


def test_nested_subagent_approvals_keep_both_action_identities_and_resume_the_original_child(tmp_path: Path) -> None:
    async def scenario() -> None:
        model = NestedApprovalModel()
        service = product_service(tmp_path, model)
        service.agents.approval_tool_codes = frozenset({"write_file"})
        space = service.store.create_space(SpaceConfiguration(name="Research"), request_id="space")
        await service.start()
        try:
            execution = await service.delegate(
                ExecutionSpec(space_id=space.space_id, objective="Write both approved notes"), "work"
            )
            async with asyncio.timeout(5):
                waiting = await service.wait(execution.execution_id)
            assert waiting.status is ExecutionStatus.WAITING_APPROVAL
            assert waiting.current_run_id is not None
            snapshot = await service.sessions.run(waiting.session_id, waiting.current_run_id)
            assert snapshot.checkpoint is not None
            parent_pending = snapshot.checkpoint.interruptions[0]
            assert parent_pending.details["kind"] == "pleiad.subagent"
            child = RunCheckpoint.model_validate(parent_pending.details["checkpoint"])
            attentions = tuple(
                value for value in service.store.attentions().items if value.execution_id == execution.execution_id
            )
            assert {value.attention_id for value in attentions} == {
                value.interruption_id for value in child.interruptions
            }
            assert len(attentions) == 1
            first_attention = attentions[0]
            for index in range(2):
                pending = tuple(
                    value for value in service.store.attentions().items if value.execution_id == execution.execution_id
                )
                assert len(pending) == 1
                attention = pending[0]
                assert attention.action_digest is not None
                await service.resolve(
                    attention.attention_id,
                    ResolutionRequest(
                        request_id=f"approve-{index}",
                        expected_revision=attention.revision,
                        decision="approve",
                        action_digest=attention.action_digest,
                    ),
                )
                if index == 0:
                    async with asyncio.timeout(5):
                        async for event in service.events():
                            if event.type == "attention.created" and event.subject != first_attention.attention_id:
                                break
                    current = await service.require_execution(execution.execution_id)
                    assert current.status is ExecutionStatus.WAITING_APPROVAL
                    following = service.store.attentions().items[0]
                    assert following.attention_id != first_attention.attention_id
                    assert following.action_digest != first_attention.action_digest
                    resumed_child = await service.sessions.run(child.session_id, child.run_id)
                    assert resumed_child.checkpoint is not None
                    assert resumed_child.checkpoint.run_id == child.run_id
                    assert {value.interruption_id for value in resumed_child.checkpoint.interruptions} == {
                        following.attention_id
                    }
                    scratch = service.agents.workspace / "work" / execution.space_id / execution.execution_id
                    assert (scratch / "first.txt").read_text() == "first"
                    assert not (scratch / "second.txt").exists()
            async with asyncio.timeout(5):
                finished = await service.wait(execution.execution_id)
            assert finished.status is ExecutionStatus.SUCCEEDED
            assert finished.budget_consumed == 4
            assert finished.current_run_id == waiting.current_run_id
            child_snapshot = await service.sessions.run(child.session_id, child.run_id)
            assert child_snapshot.receipt is not None and child_snapshot.receipt.state == "succeeded"
            calls = await service.sessions.tool_calls(child.session_id, child.run_id)
            assert [(call.call_id, call.state) for call in calls] == [
                ("write-first", "succeeded"),
                ("write-second", "succeeded"),
            ]
            journal = await service.sessions.snapshot(child.session_id)
            assert [
                value.message.tool_call_id for value in journal.messages if isinstance(value.message, ToolMessage)
            ] == ["write-first", "write-second"]
            scratch = service.agents.workspace / "work" / execution.space_id / execution.execution_id
            assert (scratch / "first.txt").read_text() == "first"
            assert (scratch / "second.txt").read_text() == "second"
        finally:
            await service.shutdown()

    asyncio.run(scenario())


class ChildFailureModel(ProductScenarioModel):
    def __init__(self) -> None:
        super().__init__()
        self.space_id = ""
        self.delegated = False
        self.release_workers.set()

    async def stream(self, request: OpenAIRequest | Mapping[str, object]) -> AsyncIterator[OpenAIStreamChunk]:
        if not isinstance(request, OpenAIRequest):
            raise TypeError("The provider requires a validated protocol request")
        if any(tool.function.name == "query_product" for tool in request.tools or []):
            async for chunk in super().stream(request):
                yield chunk
            return
        self.requests.append(request)
        is_child = "Independent child" in request.model_dump_json()
        calls: tuple[OpenAIStreamToolCall, ...] = ()
        text = None
        if not is_child and not self.delegated:
            self.delegated = True
            calls = (
                OpenAIStreamToolCall(
                    index=0,
                    id="delegate",
                    type="function",
                    function=OpenAIStreamFunction(
                        name="delegate_execution",
                        arguments=json.dumps(
                            {"space_id": self.space_id, "objective": "Independent child", "budget_limit": 1000}
                        ),
                    ),
                ),
            )
        else:
            text = json.dumps(
                {"kind": "failed", "summary": "The required evidence could not be obtained.", "handled_input_ids": []}
            )
        yield OpenAIStreamChunk(
            id=f"response-{len(self.requests)}",
            model=self.model,
            choices=(
                OpenAIStreamChoice(
                    delta=OpenAIStreamDelta(content=text, tool_calls=calls),
                    finish_reason="tool_calls" if calls else "stop",
                ),
            ),
            usage=OpenAIUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        )


def test_failed_required_child_is_consumed_and_the_parent_reports_business_failure_without_waiting_forever(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        model = ChildFailureModel()
        service = product_service(tmp_path, model)
        space = service.store.create_space(SpaceConfiguration(name="Research"), request_id="space")
        model.space_id = space.space_id
        await service.start()
        try:
            parent = await service.delegate(ExecutionSpec(space_id=space.space_id, objective="Parent plan"), "parent")
            async with asyncio.timeout(5):
                finished = await service.wait(parent.execution_id)
            assert finished.status is ExecutionStatus.FAILED
            children = [
                value
                for value in service.store.list_executions().items
                if value.parent_execution_id == parent.execution_id
            ]
            assert len(children) == 1 and children[0].status is ExecutionStatus.FAILED
            assert finished.budget_reserved == 0
            assert finished.budget_consumed == 3
            assert len(service.store.execution_runs(parent.execution_id)) == 1
            assert finished.current_run_id is not None
            snapshot = await service.sessions.run(finished.session_id, finished.current_run_id)
            assert snapshot.receipt is not None and snapshot.receipt.state == "succeeded"
            assert snapshot.receipt.usage.model_calls == 2
            reports = service.store.reports(parent.execution_id)
            assert len(reports) == 1 and reports[0].kind == "failed" and not reports[0].rejection_reasons
            child_results = [
                value for value in service.store.worker_inputs(parent.execution_id) if value.kind == "delegate_result"
            ]
            assert len(child_results) == 1 and child_results[0].causation_id == children[0].execution_id
            dispositions = await service.all_records("input_dispositions", InputDisposition)
            assert dispositions and all(value.state == "returned" for value in dispositions)
        finally:
            await service.shutdown()

    asyncio.run(scenario())


def test_cancelling_a_nested_approval_settles_the_child_receipt_and_charges_actual_scoped_usage(tmp_path: Path) -> None:
    async def scenario() -> None:
        model = NestedApprovalModel()
        service = product_service(tmp_path, model)
        service.agents.approval_tool_codes = frozenset({"write_file"})
        space = service.store.create_space(SpaceConfiguration(name="Research"), request_id="space")
        await service.start()
        try:
            execution = await service.delegate(
                ExecutionSpec(space_id=space.space_id, objective="Write both approved notes"), "work"
            )
            async with asyncio.timeout(5):
                waiting = await service.wait(execution.execution_id)
            assert waiting.status is ExecutionStatus.WAITING_APPROVAL
            assert waiting.current_run_id is not None
            snapshot = await service.sessions.run(waiting.session_id, waiting.current_run_id)
            assert snapshot.checkpoint is not None
            child = RunCheckpoint.model_validate(snapshot.checkpoint.interruptions[0].details["checkpoint"])
            await service.control(
                execution.execution_id, "cancel", request_id="cancel", expected_revision=waiting.revision
            )
            async with asyncio.timeout(5):
                finished = await service.wait(execution.execution_id)
            assert finished.status is ExecutionStatus.CANCELLED
            assert finished.budget_consumed == 2
            child_snapshot = await service.sessions.run(child.session_id, child.run_id)
            assert child_snapshot.receipt is not None and child_snapshot.receipt.state == "cancelled"
            assert {value.status for value in service.store.attentions(status=None).items} == {"withdrawn"}
            scratch = service.agents.workspace / "work" / execution.space_id / execution.execution_id
            assert not (scratch / "first.txt").exists() and not (scratch / "second.txt").exists()
        finally:
            await service.shutdown()

    asyncio.run(scenario())


async def accepted_cleanup_child(
    service: ExecutionService, session_id: str, parent_run_id: str, *, finish: bool
) -> SessionWriter:
    session = Session(session_id, service.sessions)
    await session.enqueue(InputEnvelope(input_id="input", kind="user", source="host", content="Private scoped work"))
    accepted = await service.sessions.start(
        session_id,
        invocation_id="child",
        parameters_digest=fingerprint({"session_id": session_id}),
        input_ids=("input",),
        input_mode="batch",
        agent_id="specialist",
        parent_run_id=parent_run_id,
    )
    writer = SessionWriter(session, accepted.snapshot)
    if finish:
        step = await writer.begin("specialist")
        await writer.complete(step.step_id, (AssistantMessage(content="Private child history"),))
        await writer.finish(
            RunReceipt(run_id=writer.run_id, input_ids=("input",), state="succeeded", completed_at=datetime.now(UTC))
        )
    return writer


def test_space_cleanup_retries_its_persisted_recursive_session_plan_before_deleting_owned_files(tmp_path: Path) -> None:
    async def scenario() -> None:
        model = ProductScenarioModel()
        service = product_service(tmp_path, model)
        service.agents.facts = FactStore(service.store.root, user_id="owner", cross_space=True)
        space = service.store.create_space(SpaceConfiguration(name="Research"), request_id="space")
        other = service.store.create_space(SpaceConfiguration(name="Other"), request_id="other")
        await service.start()
        try:
            execution = await service.delegate(
                ExecutionSpec(space_id=space.space_id, objective="Complete work"), "work"
            )
            async with asyncio.timeout(5):
                while True:
                    running = await service.require_execution(execution.execution_id)
                    if running.current_run_id is not None:
                        break
                    await asyncio.sleep(0)
            service.details.save_tool_result(
                ToolResultDetail(
                    tool_result_id="private-result",
                    execution_id=execution.execution_id,
                    run_id=running.current_run_id,
                    step_id="step",
                    call_id="call",
                    text="Private raw result",
                )
            )
            with service.store.transaction(write=True) as connection:
                service.store.emit(
                    connection, execution.execution_id, "node.attempt.updated", {"private": "Raw node output"}
                )
            model.release_workers.set()
            async with asyncio.timeout(5):
                finished = await service.wait(execution.execution_id)
            assert finished.status is ExecutionStatus.SUCCEEDED and finished.current_run_id is not None
            child = await accepted_cleanup_child(service, "a-child", finished.current_run_id, finish=True)
            grandchild = await accepted_cleanup_child(service, "z-grandchild", child.run_id, finish=False)
            await accepted_cleanup_child(service, "unrelated", "another-owner", finish=True)
            service.agents.facts.save_fact(
                MemoryFact(memory_id="space", scope="space", scope_id=space.space_id, content="Private Space fact")
            )
            service.agents.facts.save_fact(
                MemoryFact(memory_id="other", scope="space", scope_id=other.space_id, content="Other fact")
            )
            service.agents.facts.save_fact(
                MemoryFact(memory_id="user", scope="user", scope_id="owner", content="Shared user fact")
            )
            work = service.agents.workspace / "work" / space.space_id / execution.execution_id
            work.mkdir(parents=True, exist_ok=True)
            (work / "private.txt").write_text("Private execution bytes")
            other_work = service.agents.workspace / "work" / other.space_id / "other-work"
            other_work.mkdir(parents=True)
            (other_work / "keep.txt").write_text("Unrelated bytes")
            (work / "other-link").symlink_to(other_work, target_is_directory=True)
            blocked_preview = await service.deletion_preview(space.space_id)
            assert "Active or paused SDK scopes still belong to this Space" in blocked_preview.blockers
            await grandchild.finish(
                RunReceipt(
                    run_id=grandchild.run_id, input_ids=("input",), state="cancelled", completed_at=datetime.now(UTC)
                )
            )
            async with asyncio.timeout(5):
                while True:
                    preview = await service.deletion_preview(space.space_id)
                    if not preview.blockers:
                        break
                    await asyncio.sleep(0)
            assert preview.scopes.sdk_session_count == 3
            assert preview.scopes.task_file_count == 2
            assert preview.scopes.space_fact_count == 1
            service.content.accept_deletion(
                space.space_id,
                request_id="delete",
                expected_revision=preview.space_revision,
                preview_token=preview.preview_token,
                scopes=preview.scopes,
            )
            # Inject an orphaned SDK admission after the product's accepted cleanup intent.
            await Session("z-grandchild", service.sessions).enqueue(
                InputEnvelope(input_id="late", kind="user", source="host", content="Late admitted work")
            )
            late = await service.sessions.start(
                "z-grandchild",
                invocation_id="late",
                parameters_digest=fingerprint({"late": True}),
                input_ids=("late",),
                input_mode="batch",
                agent_id="specialist",
                parent_run_id=child.run_id,
            )
            grandchild = SessionWriter(Session("z-grandchild", service.sessions), late.snapshot)
            with pytest.raises(ProductConflict, match="failed and can be retried"):
                await service.delete_space(
                    space.space_id,
                    request_id="delete",
                    expected_revision=preview.space_revision,
                    preview_token=preview.preview_token,
                )
            deleting = service.store.get_space(space.space_id)
            assert deleting is not None and deleting.cleanup_generation is not None
            failed = service.content.cleanup_record("delete", expected_generation=deleting.cleanup_generation)
            assert failed.state == "failed" and set(failed.session_ids) == {
                execution.session_id,
                "a-child",
                "z-grandchild",
            }
            assert service.ready and not (await service.sessions.snapshot("a-child")).runs
            assert (await service.sessions.snapshot("z-grandchild")).current_run_id == grandchild.run_id
            with pytest.raises(ProductConflict, match="generation is obsolete"):
                service.content.reclaim_work_directory(
                    "delete", expected_generation="obsolete", workspace=service.agents.workspace
                )
            assert (work / "private.txt").read_text() == "Private execution bytes"
            await grandchild.finish(
                RunReceipt(
                    run_id=grandchild.run_id, input_ids=("late",), state="cancelled", completed_at=datetime.now(UTC)
                )
            )
            result = await service.delete_space(
                space.space_id,
                request_id="delete",
                expected_revision=preview.space_revision,
                preview_token=preview.preview_token,
            )
            assert result == {"deleted": True}
            assert not (await service.sessions.snapshot("z-grandchild")).runs
            assert (await service.sessions.snapshot("unrelated")).runs
            assert not (service.agents.workspace / "work" / space.space_id).exists()
            assert (other_work / "keep.txt").read_text() == "Unrelated bytes"
            assert service.agents.facts.list_facts("space", space_id=space.space_id) == ()
            assert service.agents.facts.list_facts("space", space_id=other.space_id)[0].content == "Other fact"
            assert service.agents.facts.list_facts("user")[0].content == "Shared user fact"
            with pytest.raises(LookupError, match="deleted"):
                service.details.tool_result_page(execution.execution_id, "private-result")
            assert service.store.reports(execution.execution_id)[0].summary == "Completed the requested work."
            events = service.store.events(limit=100).items
            node_events = [value for value in events if value.type == "node.attempt.updated"]
            assert node_events and all(value.data.get("deleted") is True for value in node_events)
        finally:
            model.release_workers.set()
            await service.shutdown()

    asyncio.run(scenario())


def test_changed_controlled_task_files_invalidate_the_reviewed_space_deletion_scope(tmp_path: Path) -> None:
    async def scenario() -> None:
        service = product_service(tmp_path, ProductScenarioModel())
        space = service.store.create_space(SpaceConfiguration(name="Research"), request_id="space")
        directory = service.agents.workspace / "work" / space.space_id
        directory.mkdir(parents=True)
        (directory / "result.txt").write_text("First result")
        await service.start()
        try:
            preview = await service.deletion_preview(space.space_id)
            assert preview.scopes.task_file_count == 1
            (directory / "result.txt").write_text("Changed result")
            with pytest.raises(ProductConflict, match="impact changed"):
                await service.delete_space(
                    space.space_id,
                    request_id="delete",
                    expected_revision=preview.space_revision,
                    preview_token=preview.preview_token,
                )
            current = service.store.get_space(space.space_id)
            assert current is not None and not current.deleted and not current.deletion_pending
            assert (directory / "result.txt").read_text() == "Changed result"
        finally:
            await service.shutdown()

    asyncio.run(scenario())


class CorrectedDeliveryModel(ProductScenarioModel):
    def __init__(self) -> None:
        super().__init__()
        self.corrected_input_ids: tuple[str, ...] = ()
        self.correction_started = asyncio.Event()
        self.release_correction = asyncio.Event()
        self.release_workers.set()

    async def stream(self, request: OpenAIRequest | Mapping[str, object]) -> AsyncIterator[OpenAIStreamChunk]:
        if not isinstance(request, OpenAIRequest):
            raise TypeError("The provider requires a validated protocol request")
        if self.corrected_input_ids and any(tool.function.name == "ask_user" for tool in request.tools or []):
            self.requests.append(request)
            self.correction_started.set()
            await self.release_correction.wait()
            yield OpenAIStreamChunk(
                id="corrected-delivery",
                model=self.model,
                choices=(
                    OpenAIStreamChoice(
                        delta=OpenAIStreamDelta(
                            content=json.dumps(
                                {
                                    "summary": "Both requirements are delivered.",
                                    "handled_input_ids": self.corrected_input_ids,
                                }
                            )
                        ),
                        finish_reason="stop",
                    ),
                ),
                usage=OpenAIUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            )
            return
        async for chunk in super().stream(request):
            yield chunk


def test_a_new_input_after_sdk_success_rejects_the_old_report_and_continues_the_same_execution_session(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        model = CorrectedDeliveryModel()
        service = product_service(tmp_path, model)
        space = service.store.create_space(SpaceConfiguration(name="Research"), request_id="space")
        execution = service.store.create_execution(
            ExecutionSpec(space_id=space.space_id, objective="Complete accepted work"), request_id="work"
        )
        original = service.store.seed_execution_input(execution.execution_id)
        await service.deliver_inputs()
        service.store.transition_execution(
            execution.execution_id, ExecutionStatus.RUNNING, expected_revision=execution.revision
        )
        mapping = ExecutionRun(execution_id=execution.execution_id, attempt=1, invocation_id="first")
        service.store.record_execution_run(mapping)
        binding = await service.agents.worker(execution, service.ignore_event, service.delegate, service.progress)
        binding.ready.set()
        handle = await binding.runner.start(
            binding.agent,
            session=Session(execution.session_id, service.sessions),
            invocation_id="first",
            input_ids=(original.input_id,),
            input_mode="live",
            limits=RunLimits(execution_timeout_seconds=1800, max_model_steps=64, max_output_tokens=100_000),
            context=binding.context,
        )
        completed = await handle.result()
        assert completed.state.value == "succeeded"
        service.store.record_execution_run(mapping.model_copy(update={"run_id": handle.run_id, "state": "running"}))
        await binding.runner.aclose()
        supplement = service.store.submit_message(
            ChatMessageRequest(
                client_message_id="late",
                text="Include another requirement",
                target_execution_ids=(execution.execution_id,),
            )
        )
        model.corrected_input_ids = (original.input_id, supplement.input_ids[0])
        await service.start()
        try:
            async with asyncio.timeout(5):
                await model.correction_started.wait()
            reports = service.store.reports(execution.execution_id)
            assert len(reports) == 1 and reports[0].rejection_reasons
            assert "New accepted input" in reports[0].rejection_reasons[0]
            dispositions = await service.all_records("input_dispositions", InputDisposition)
            assert len(dispositions) == 2 and all(value.state == "owned" for value in dispositions)
            observations = [
                value
                for value in service.store.worker_inputs(execution.execution_id)
                if value.source == "weave.validation"
            ]
            assert len(observations) == 1 and reports[0].report_id in observations[0].content
            assert all(identity in observations[0].content for identity in model.corrected_input_ids)
            model.release_correction.set()
            async with asyncio.timeout(5):
                finished = await service.wait(execution.execution_id)
            assert finished.status is ExecutionStatus.SUCCEEDED and finished.validation_attempts == 1
            runs = service.store.execution_runs(execution.execution_id)
            assert len(runs) == 2 and len({value.run_id for value in runs}) == 2
            assert finished.session_id == execution.session_id and finished.budget_consumed == 2
            journal = await service.sessions.snapshot(execution.session_id)
            assert sum(message.message.message_id == original.input_id for message in journal.messages) == 1
            reports = service.store.reports(execution.execution_id)
            assert len(reports) == 2 and not reports[-1].rejection_reasons
            dispositions = await service.all_records("input_dispositions", InputDisposition)
            assert all(value.state == "handled" for value in dispositions)
        finally:
            model.release_correction.set()
            await service.shutdown()

    asyncio.run(scenario())


def test_an_accepted_attention_answer_resumes_its_worker_even_when_the_silent_router_queue_is_full(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        service = product_service(tmp_path, ClarificationScenarioModel())
        service.store.queue_capacity = 1
        space = service.store.create_space(SpaceConfiguration(name="Research"), request_id="space")
        await service.start()
        try:
            execution = await service.delegate(
                ExecutionSpec(space_id=space.space_id, objective="Write a report"), "work"
            )
            async with asyncio.timeout(5):
                waiting = await service.wait(execution.execution_id)
            assert waiting.status is ExecutionStatus.WAITING_INPUT
            with pytest.raises(ProductCapacityError):
                await service.submit(ChatMessageRequest(client_message_id="overflow", text="Another request"))
            attention = service.store.attentions().items[0]
            request = ResolutionRequest(request_id="answer", expected_revision=attention.revision, answer="Engineering")
            receipt = await service.resolve(attention.attention_id, request)
            assert await service.resolve(attention.attention_id, request) == receipt
            async with asyncio.timeout(5):
                finished = await service.wait(execution.execution_id)
            assert finished.status is ExecutionStatus.SUCCEEDED and finished.current_run_id == waiting.current_run_id
            assert len(service.store.execution_runs(execution.execution_id)) == 1
            dispositions = await service.all_records("input_dispositions", InputDisposition)
            assert len(dispositions) == 2 and all(value.state == "handled" for value in dispositions)
            messages = service.store.messages().items
            assert [value.body for value in messages if value.role == "user"] == ["Engineering"]
        finally:
            await service.shutdown()

    asyncio.run(scenario())


class ClosingRouterModel(ProductScenarioModel):
    def __init__(self) -> None:
        super().__init__()
        self.router_started = asyncio.Event()
        self.release_router = asyncio.Event()

    async def stream(self, request: OpenAIRequest | Mapping[str, object]) -> AsyncIterator[OpenAIStreamChunk]:
        if not isinstance(request, OpenAIRequest):
            raise TypeError("The provider requires a validated protocol request")
        if any(tool.function.name == "query_product" for tool in request.tools or []):
            self.router_started.set()
            await self.release_router.wait()
        async for chunk in super().stream(request):
            yield chunk


def test_controlled_shutdown_settles_the_accepted_router_result_without_spending_another_attempt(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        model = ClosingRouterModel()
        service = product_service(tmp_path, model)
        await service.start()
        try:
            await service.submit(ChatMessageRequest(client_message_id="query", text="status"))
            async with asyncio.timeout(5):
                await model.router_started.wait()
                service.wakeup.clear()
                closing = asyncio.create_task(service.shutdown())
                await service.wakeup.wait()
                assert service.closing
                model.release_router.set()
                await closing
            attempts = await service.all_records("route_attempts", RouteAttempt)
            assert len(attempts) == 1 and attempts[0].attempt == 1 and attempts[0].state == "applied"
            assert attempts[0].failure_reason is None
            messages = service.store.messages().items
            assert [value.body for value in messages if value.role == "assistant"] == ["Both Workers are running."]
            assert not any(value.type == "route.failed" for value in service.store.events().items)
            assert service.owner_descriptor is None and not service.ready
        finally:
            model.release_router.set()
            if service.owner_descriptor is not None:
                await service.shutdown()

    asyncio.run(scenario())


class ResolvedHandbackModel(ProductScenarioModel):
    def __init__(self, space_id: str) -> None:
        super().__init__()
        self.space_id = space_id
        self.routed = False
        self.release_workers.set()

    async def stream(self, request: OpenAIRequest | Mapping[str, object]) -> AsyncIterator[OpenAIStreamChunk]:
        if not isinstance(request, OpenAIRequest):
            raise TypeError("The provider requires a validated protocol request")
        if not self.routed and any(tool.function.name == "query_product" for tool in request.tools or []):
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
            decoded: object = json.JSONDecoder().raw_decode(joined[joined.index('{"inputs":') :])[0]
            inputs = JSON_OBJECT.validate_python(decoded)["inputs"]
            assert isinstance(inputs, list) and inputs and isinstance(inputs[0], dict)
            identity = inputs[0]["input_id"]
            assert isinstance(identity, str)
            self.routed = True
            self.requests.append(request)
            output = {
                "reply": "The original request can continue after its remote effect was reconciled.",
                "actions": [
                    {
                        "kind": "dispatch",
                        "space_ref": {"kind": "existing", "id": self.space_id},
                        "objective": "Continue the original request",
                        "source_input_ids": [identity],
                    }
                ],
            }
            yield OpenAIStreamChunk(
                id="continue-request",
                model=self.model,
                choices=(
                    OpenAIStreamChoice(delta=OpenAIStreamDelta(content=json.dumps(output)), finish_reason="stop"),
                ),
                usage=OpenAIUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            )
            return
        async for chunk in super().stream(request):
            yield chunk


def test_trusted_reconciliation_releases_an_unknown_handback_once_and_preserves_its_original_declaration(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        service = product_service(tmp_path, ProductScenarioModel())
        space = service.store.create_space(SpaceConfiguration(name="Research"), request_id="space")
        model = ResolvedHandbackModel(space.space_id)
        service.agents.models = {"text": model}
        execution = service.store.create_execution(
            ExecutionSpec(space_id=space.space_id, objective="Submit remote work"), request_id="work"
        )
        accepted = service.store.submit_message(
            ChatMessageRequest(
                client_message_id="work", text="Submit remote work", target_execution_ids=(execution.execution_id,)
            )
        )
        item = service.store.get_input(accepted.input_ids[0])
        assert item is not None
        await service.deliver_inputs()
        started = await service.sessions.start(
            execution.session_id,
            invocation_id="lost",
            parameters_digest=fingerprint({"lost": True}),
            input_ids=(item.input_id,),
            input_mode="live",
            agent_id="worker",
        )
        writer = SessionWriter(Session(execution.session_id, service.sessions), started.snapshot)
        step = await writer.begin("worker")
        await writer.append(
            step.step_id, (AssistantMessage(tool_calls=(ToolCall(id="remote", name="remote_action", arguments="{}"),)),)
        )
        await writer.save_tool_call(
            ToolCallRecord(
                run_id=writer.run_id,
                step_id=step.step_id,
                call_id="remote",
                tool_code="remote_action",
                binding_digest="binding",
                arguments_digest="arguments",
                authorization_ref="host",
                state="unknown",
            )
        )
        service.store.transition_execution(
            execution.execution_id, ExecutionStatus.RUNNING, expected_revision=execution.revision
        )
        service.store.record_execution_run(
            ExecutionRun(
                execution_id=execution.execution_id,
                attempt=1,
                invocation_id="lost",
                run_id=writer.run_id,
                state="running",
            )
        )
        handback = service.store.return_to_router(
            item.input_id,
            execution_id=execution.execution_id,
            expected_revision=1,
            reason="Reconcile this remote effect first",
            effect_refs=("remote",),
            unknown_effects=True,
            action_refs=(ActionRef(run_id=writer.run_id, call_id="remote"),),
        )
        await service.start()
        try:
            blocked = await service.require_execution(execution.execution_id)
            assert blocked.status is ExecutionStatus.BLOCKED
            assert any(
                value.kind == "recovery" and value.execution_id is None for value in service.store.attentions().items
            )
            assert model.requests == []
            await service.reconcile_execution(
                blocked,
                evidence=(
                    ActionEvidence(
                        evidence_id="backend-confirmed",
                        action=ActionRef(run_id=writer.run_id, call_id="remote"),
                        binding_digest="binding",
                        arguments_digest="arguments",
                        backend_evidence_ref="remote:receipt",
                        observed_at=datetime.now(UTC),
                        observed_state="succeeded",
                        result_ref="remote:receipt",
                    ),
                ),
            )
            async with asyncio.timeout(5):
                async for event in service.events():
                    if (
                        event.type == "execution.updated"
                        and event.subject != execution.execution_id
                        and event.data.get("status") == "succeeded"
                    ):
                        break
            jobs = service.store.list_executions().items
            assert len(jobs) == 2
            assert model.routed
            original = next(
                value
                for value in await service.all_records("input_handbacks", Handback)
                if value.handback_id == handback.handback_id
            )
            assert original == handback and original.unknown_effects
            resolutions = await service.all_records("input_handback_resolutions", HandbackResolution)
            assert len(resolutions) == 1 and resolutions[0].evidence_ids == ("backend-confirmed",)
            assert resolutions[0].action_refs == handback.action_refs
            projected = await service.execution_detail(execution.execution_id)
            assert projected["handback_resolutions"] == [resolutions[0].model_dump(mode="json")]
            source = next(value for value in service.store.messages().items if value.message_id == accepted.message_id)
            assert source.handbacks[0].unknown_effects
            assert source.handback_resolutions == resolutions
            assert not any(value.kind == "recovery" for value in service.store.attentions().items)
            journal = await service.sessions.snapshot(execution.session_id)
            record = next(
                value for value in journal.reconciliations if value.request_id == resolutions[0].reconciliation_id
            )
            assert (
                service.store.settle_handbacks(
                    execution.execution_id, reconciliation=record, actions=journal.tool_calls
                )
                == ()
            )
            inputs = await service.all_records("inbox_items", InboxItem)
            assert (
                sum(
                    value.source == "weave.handback" and value.origin_input_id == item.origin_input_id
                    for value in inputs
                )
                == 1
            )
        finally:
            await service.shutdown()

    asyncio.run(scenario())


class ApprovalProcessSandbox(OciSandbox):
    def __init__(self, exit_code: int = 0) -> None:
        super().__init__()
        self.exit_code = exit_code
        self.requests: list[SandboxRequest] = []
        self.accepted = asyncio.Event()
        self.release = asyncio.Event()

    async def execute(
        self, request: SandboxRequest, *, on_accept: Callable[[SandboxReceipt], Awaitable[None]] | None = None
    ) -> SandboxResult:
        """Hold an external process after its real backend acceptance callback commits."""
        if request.operation_id is None or on_accept is None:
            raise ValueError("The approved command must retain concrete background admission")
        self.requests.append(request)
        await on_accept(
            SandboxReceipt(
                runtime="docker",
                operation_id=request.operation_id,
                container_name=f"approved-{request.operation_id}",
                container_id=f"container-{request.operation_id}",
                admission_token=request.operation_id,
                request_digest=request.digest(),
            )
        )
        self.accepted.set()
        await self.release.wait()
        return SandboxResult(exit_code=self.exit_code, backend="approval-process", stdout=b"Approved command completed")


class ApprovedCommandModel(ProductScenarioModel):
    def __init__(self) -> None:
        super().__init__()
        self.command_requested = False
        self.release_workers.set()

    async def stream(self, request: OpenAIRequest | Mapping[str, object]) -> AsyncIterator[OpenAIStreamChunk]:
        if not isinstance(request, OpenAIRequest):
            raise TypeError("The provider requires a validated protocol request")
        worker = any(tool.function.name == "ask_user" for tool in request.tools or [])
        if worker and not self.command_requested:
            self.command_requested = True
            self.requests.append(request)
            yield OpenAIStreamChunk(
                id="approved-command",
                model=self.model,
                choices=(
                    OpenAIStreamChoice(
                        delta=OpenAIStreamDelta(
                            tool_calls=(
                                OpenAIStreamToolCall(
                                    index=0,
                                    id="command-call",
                                    type="function",
                                    function=OpenAIStreamFunction(
                                        name="command", arguments='{"command":"printf approved"}'
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
        if worker and "weave.approval_rejected" in request.model_dump_json():
            self.requests.append(request)
            yield OpenAIStreamChunk(
                id="rejected-command",
                model=self.model,
                choices=(
                    OpenAIStreamChoice(
                        delta=OpenAIStreamDelta(
                            content=json.dumps(
                                {
                                    "kind": "failed",
                                    "summary": "The owner rejected the command.",
                                    "handled_input_ids": [],
                                }
                            )
                        ),
                        finish_reason="stop",
                    ),
                ),
                usage=OpenAIUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            )
            return
        async for chunk in super().stream(request):
            yield chunk


@pytest.mark.parametrize(("decision", "exit_code"), [("approve", 0), ("approve", 23), ("reject", 0)])
def test_command_authorization_precedes_concrete_background_admission_and_keeps_one_operation(
    tmp_path: Path, decision: str, exit_code: int
) -> None:
    async def scenario() -> None:
        model = ApprovedCommandModel()
        service = product_service(tmp_path, model)
        backend = ApprovalProcessSandbox(exit_code)
        service.agents.sandbox = Sandbox(backend)
        service.agents.approval_tool_codes = frozenset({"command"})
        space = service.store.create_space(SpaceConfiguration(name="Commands"), request_id="space")
        await service.start()
        try:
            execution = await service.delegate(
                ExecutionSpec(space_id=space.space_id, objective="Run the approved command"), "work"
            )
            async with asyncio.timeout(5):
                waiting = await service.wait(execution.execution_id)
            assert waiting.status is ExecutionStatus.WAITING_APPROVAL and waiting.current_run_id is not None
            snapshot = await service.sessions.run(waiting.session_id, waiting.current_run_id)
            assert snapshot.checkpoint is not None and snapshot.checkpoint.interruptions[0].phase == "authorization"
            assert backend.requests == []
            assert await service.sessions.operations(waiting.session_id, waiting.current_run_id) == ()
            attention = service.store.attentions().items[0]
            await service.resolve(
                attention.attention_id,
                ResolutionRequest(
                    request_id="decision",
                    expected_revision=attention.revision,
                    decision=decision,
                    action_digest=attention.action_digest,
                ),
            )
            if decision == "approve":
                async with asyncio.timeout(5):
                    await backend.accepted.wait()
                    handle = service.handles[execution.execution_id]
                    async for event in handle.events():
                        message = event.event.get("message")
                        if (
                            isinstance(message, dict)
                            and message.get("role") == "tool"
                            and message.get("tool_call_id") == "command-call"
                        ):
                            break
                operations = await service.sessions.operations(waiting.session_id, waiting.current_run_id)
                assert len(operations) == 1 and operations[0].state == "running"
                assert operations[0].backend_receipt is not None
                assert operations[0].operation_id == backend.requests[0].operation_id
                assert operations[0].backend_receipt.reference == f"container-{operations[0].operation_id}"
                backend.release.set()
            async with asyncio.timeout(5):
                finished = await service.wait(execution.execution_id)
            assert finished.status is (ExecutionStatus.SUCCEEDED if decision == "approve" else ExecutionStatus.FAILED)
            assert finished.current_run_id == waiting.current_run_id
            assert len(service.store.execution_runs(execution.execution_id)) == 1
            journal = await service.sessions.snapshot(waiting.session_id)
            tool_messages = [
                value.message
                for value in journal.messages
                if isinstance(value.message, ToolMessage) and value.message.tool_call_id == "command-call"
            ]
            assert len(tool_messages) == 1
            if decision == "approve":
                assert len(backend.requests) == 1
                started_result = JSON_OBJECT.validate_json(tool_messages[0].content)["output"]
                assert isinstance(started_result, dict) and started_result["status"] == "started"
                assert len(journal.operations) == 1 and journal.operations[0].state == "succeeded"
                result = journal.operations[0].result
                assert result is not None and result.ok and result.output is not None
                assert result.output["exit_code"] == exit_code
                results = [value for value in journal.inputs if value.envelope.kind == "operation_result"]
                assert len(results) == 1 and results[0].step_id is not None
            else:
                assert backend.requests == [] and journal.operations == ()
        finally:
            backend.release.set()
            await service.shutdown()

    asyncio.run(scenario())


class RecoveryProcess:
    def __init__(
        self,
        receipt: SandboxReceipt,
        state: str,
        *,
        observed_digest: str | None = None,
        exit_code: int = 0,
        completes_on_stop: bool = False,
    ) -> None:
        self.receipt = receipt
        self.state = state
        self.observed_digest = receipt.request_digest if observed_digest is None else observed_digest
        self.exit_code = exit_code
        self.completes_on_stop = completes_on_stop
        self.queries = 0
        self.commands: list[tuple[str, ...]] = []
        self.removed = False

    async def __call__(
        self,
        command: tuple[str, ...],
        *,
        stdin: bytes | None,
        output_limit: int,
        timeout_seconds: float | None,
    ) -> ProcessResult:
        self.commands.append(command)
        if command[1] == "rm":
            assert command[-1] == self.receipt.container_id
            self.removed = True
            return ProcessResult(0, b"", b"", False, 0, False)
        if command[1] != "inspect":
            raise AssertionError(f"Recovery must not replay this backend action {command}")
        if self.removed or self.state == "missing":
            return ProcessResult(1, b"", b"Error: No such container", False, 0, False)
        self.queries += 1
        if self.completes_on_stop and self.queries == 3:
            self.state = "exited"
        payload = {
            "Id": self.receipt.container_id,
            "Name": f"/{self.receipt.container_name}",
            "Config": {
                "Labels": {
                    "com.pleiad.sandbox.owner": "pleiad-sdk",
                    "com.pleiad.sandbox.owner.operation": self.receipt.operation_id,
                    "com.pleiad.sandbox.owner.request": self.receipt.admission_token,
                    "com.pleiad.sandbox.owner.digest": self.observed_digest,
                }
            },
            "State": {
                "Status": "running" if self.state == "running" else "exited",
                "Running": self.state == "running",
                "ExitCode": self.exit_code,
            },
        }
        return ProcessResult(0, json.dumps(payload).encode(), b"", False, 0, False)


@pytest.mark.parametrize("saved_receipt", [False, True])
@pytest.mark.parametrize("exit_code", [0, 23])
def test_restart_uses_actual_oci_receipts_to_settle_a_lost_command_without_replaying_it(
    tmp_path: Path, saved_receipt: bool, exit_code: int
) -> None:
    async def scenario() -> None:
        model = ProductScenarioModel()
        service = product_service(tmp_path, model)
        space = service.store.create_space(SpaceConfiguration(name="Recovery"), request_id="space")
        execution = service.store.create_execution(
            ExecutionSpec(space_id=space.space_id, objective="Recover the original command"), request_id="work"
        )
        item = service.store.seed_execution_input(execution.execution_id)
        await service.deliver_inputs()
        accepted = await service.sessions.start(
            execution.session_id,
            invocation_id="lost-command",
            parameters_digest=fingerprint({"recovery": True}),
            input_ids=(item.input_id,),
            input_mode="live",
            agent_id="worker",
        )
        writer = SessionWriter(Session(execution.session_id, service.sessions), accepted.snapshot)
        receipt = await stage_command(service, writer, saved_receipt=saved_receipt)
        process = RecoveryProcess(receipt, "exited", exit_code=exit_code)
        service.agents.sandbox = Sandbox(OciSandbox(process_executor=process))
        service.store.transition_execution(
            execution.execution_id, ExecutionStatus.RUNNING, expected_revision=execution.revision
        )
        service.store.record_execution_run(
            ExecutionRun(
                execution_id=execution.execution_id,
                attempt=1,
                invocation_id="lost-command",
                run_id=writer.run_id,
                state="running",
            )
        )
        await service.start()
        try:
            current = await service.require_execution(execution.execution_id)
            assert current.status is ExecutionStatus.FAILED and current.unknown_effects == ()
            journal = await service.sessions.snapshot(execution.session_id)
            assert journal.operations[0].state == "succeeded"
            assert journal.operations[0].backend_receipt is not None
            assert journal.operations[0].backend_receipt.details == receipt.model_dump(mode="json")
            assert journal.tool_calls[0].state == "succeeded"
            assert journal.tool_calls[0].result is not None and journal.tool_calls[0].result.ok
            assert journal.tool_calls[0].result.output is not None
            assert journal.tool_calls[0].result.output["exit_code"] == exit_code
            assert journal.tool_calls[0].result.output["backend_state"] == ("succeeded" if exit_code == 0 else "failed")
            assert "stdout" not in journal.tool_calls[0].result.output
            assert "timed_out" not in journal.tool_calls[0].result.output
            assert {command[1] for command in process.commands} == {"inspect"}
            assert model.requests == []
            assert len(service.store.execution_runs(execution.execution_id)) == 1
        finally:
            await service.shutdown()

    asyncio.run(scenario())


async def stage_command(service: ExecutionService, writer: SessionWriter, *, saved_receipt: bool) -> SandboxReceipt:
    """Persist the original command intention and its backend submission boundary."""
    step = await writer.begin("worker")
    await writer.append(
        step.step_id,
        (AssistantMessage(tool_calls=(ToolCall(id="command-call", name="command", arguments='{"command":"true"}'),)),),
    )
    await writer.save_tool_call(
        ToolCallRecord(
            run_id=writer.run_id,
            step_id=step.step_id,
            call_id="command-call",
            tool_code="command",
            binding_digest=fingerprint({"original_binding": "command"}),
            arguments_digest=fingerprint({"command": "true"}),
            authorization_ref="host",
        )
    )
    operation_id = f"operation-{writer.run_id}"
    request = SandboxRequest(
        cwd=PurePosixPath("/pleiad"),
        shell_command="true",
        policy=service.agents.sandbox_policy,
        operation_id=operation_id,
    )
    identity = create_container_identity(operation_id)
    receipt = SandboxReceipt(
        runtime="docker",
        operation_id=operation_id,
        container_name=identity.name,
        container_id=f"container-{writer.run_id}",
        admission_token=identity.admission_token,
        request_digest=request.digest(),
    )
    prepared = OperationRecord(
        operation_id=operation_id,
        run_id=writer.run_id,
        call_id="command-call",
        state="prepared",
        submission=ToolSubmission(operation_id=operation_id, request_digest=request.digest()),
    )
    await writer.save_operation(prepared)
    if saved_receipt:
        await writer.save_operation(
            OperationRecord(
                operation_id=operation_id,
                run_id=writer.run_id,
                call_id="command-call",
                state="running",
                submission=prepared.submission,
                backend_receipt=ToolAcceptance(
                    operation_id=operation_id,
                    backend=receipt.runtime,
                    reference=receipt.container_id,
                    request_digest=request.digest(),
                    details=receipt.model_dump(mode="json"),
                ),
            )
        )
    return receipt


@pytest.mark.parametrize("backend_state", ["running", "missing", "mismatched_request", "completes_during_stop"])
def test_cancel_reconciles_a_lost_nested_command_through_its_original_parent_ownership(
    tmp_path: Path, backend_state: str
) -> None:
    async def scenario() -> None:
        model = ProductScenarioModel()
        service = product_service(tmp_path, model)
        space = service.store.create_space(SpaceConfiguration(name="Nested recovery"), request_id="space")
        execution = service.store.create_execution(
            ExecutionSpec(space_id=space.space_id, objective="Cancel the original nested operation"), request_id="work"
        )
        item = service.store.seed_execution_input(execution.execution_id)
        await service.deliver_inputs()
        root = await service.sessions.start(
            execution.session_id,
            invocation_id="root-command",
            parameters_digest=fingerprint({"root": True}),
            input_ids=(item.input_id,),
            input_mode="live",
            agent_id="worker",
        )
        await Session("nested-command", service.sessions).enqueue(
            InputEnvelope(
                input_id="nested-task", kind="observation", source="host", content="Execute the original node"
            )
        )
        nested = await service.sessions.start(
            "nested-command",
            invocation_id="nested-command",
            parameters_digest=fingerprint({"nested": True}),
            input_ids=("nested-task",),
            input_mode="batch",
            agent_id="node",
            parent_run_id=root.snapshot.run_id,
        )
        writer = SessionWriter(Session("nested-command", service.sessions), nested.snapshot)
        receipt = await stage_command(service, writer, saved_receipt=False)
        process = RecoveryProcess(
            receipt,
            "missing" if backend_state == "missing" else "running",
            observed_digest=fingerprint({"different": True}) if backend_state == "mismatched_request" else None,
            exit_code=23 if backend_state == "completes_during_stop" else 0,
            completes_on_stop=backend_state == "completes_during_stop",
        )
        service.agents.sandbox = Sandbox(OciSandbox(process_executor=process))
        service.store.transition_execution(
            execution.execution_id, ExecutionStatus.RUNNING, expected_revision=execution.revision
        )
        service.store.record_execution_run(
            ExecutionRun(
                execution_id=execution.execution_id,
                attempt=1,
                invocation_id="root-command",
                run_id=root.snapshot.run_id,
                state="running",
            )
        )
        await service.start()
        try:
            blocked = await service.require_execution(execution.execution_id)
            assert blocked.status is ExecutionStatus.BLOCKED
            assert blocked.unknown_effects == ("command-call",)
            await service.control(
                execution.execution_id, "cancel", request_id="cancel", expected_revision=blocked.revision
            )
            async with asyncio.timeout(5):
                async for event in service.events():
                    if event.subject == execution.execution_id and event.data.get("status") in {"cancelled", "blocked"}:
                        current = await service.require_execution(execution.execution_id)
                        if current.control_intent == "cancel" and current.status in {
                            ExecutionStatus.CANCELLED,
                            ExecutionStatus.BLOCKED,
                        }:
                            break
            current = await service.require_execution(execution.execution_id)
            nested_journal = await service.sessions.snapshot("nested-command")
            root_run = await service.sessions.run(execution.session_id, root.snapshot.run_id)
            assert root_run.receipt is not None and root_run.receipt.reason == "interrupted"
            assert nested_journal.runs[0].receipt is not None
            assert {command[1] for command in process.commands} <= {"inspect", "rm"}
            assert model.requests == []
            if backend_state in {"running", "completes_during_stop"}:
                assert current.status is ExecutionStatus.CANCELLED and current.unknown_effects == ()
                assert process.removed
                expected = "succeeded" if backend_state == "completes_during_stop" else "cancelled"
                assert nested_journal.operations[0].state == expected
                assert nested_journal.tool_calls[0].state == expected
                if backend_state == "completes_during_stop":
                    result = nested_journal.tool_calls[0].result
                    assert result is not None and result.ok and result.output is not None
                    assert result.output["exit_code"] == 23 and result.output["backend_state"] == "failed"
                assert sum(command[1] == "rm" for command in process.commands) == 1
            else:
                assert current.status is ExecutionStatus.BLOCKED and current.control_intent == "cancel"
                assert current.unknown_effects == ("command-call",)
                assert not process.removed
                assert nested_journal.operations[0].state == "prepared"
        finally:
            await service.shutdown()

    asyncio.run(scenario())


class ParentCancellationModel(ProductScenarioModel):
    def __init__(self) -> None:
        super().__init__()
        self.started = {
            name: asyncio.Event() for name in ("Parent model barrier", "Independent one", "Independent two")
        }
        self.parent_cancelled = asyncio.Event()
        self.release_parent_cleanup = asyncio.Event()

    async def stream(self, request: OpenAIRequest | Mapping[str, object]) -> AsyncIterator[OpenAIStreamChunk]:
        if not isinstance(request, OpenAIRequest):
            raise TypeError("The model requires its validated provider request")
        worker = any(tool.function.name == "ask_user" for tool in request.tools or [])
        name = next((value for value in self.started if value in request.model_dump_json()), None) if worker else None
        if name is not None:
            self.started[name].set()
        try:
            async for chunk in super().stream(request):
                yield chunk
        except asyncio.CancelledError:
            if name == "Parent model barrier":
                self.parent_cancelled.set()
                await self.release_parent_cleanup.wait()
            raise


def test_parent_cancel_interrupts_a_blocked_model_and_settles_queued_children_without_stalling_other_roots(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        model = ParentCancellationModel()
        service = product_service(tmp_path, model)
        space = service.store.create_space(SpaceConfiguration(name="Cancellation"), request_id="space")
        await service.start()
        try:
            roots = [
                await service.delegate(ExecutionSpec(space_id=space.space_id, objective=objective), objective)
                for objective in ("Parent model barrier", "Independent one", "Independent two")
            ]
            async with asyncio.timeout(5):
                for name in ("Parent model barrier", "Independent one", "Independent two"):
                    await model.started[name].wait()
            parent = roots[0]
            children = [
                service.store.create_execution(
                    ExecutionSpec(
                        space_id=space.space_id,
                        parent_execution_id=parent.execution_id,
                        objective=objective,
                        budget_limit=100,
                    ),
                    request_id=objective,
                )
                for objective in ("Queued child one", "Queued child two")
            ]
            for child in children:
                service.store.seed_execution_input(child.execution_id)
            service.wakeup.set()
            current = await service.require_execution(parent.execution_id)
            assert current.budget_reserved == 200
            handle = service.handles[parent.execution_id]
            await service.control(
                parent.execution_id, "cancel", request_id="cancel-parent", expected_revision=current.revision
            )
            async with asyncio.timeout(5):
                await model.parent_cancelled.wait()
                async for event in service.events():
                    if event.data.get("status") == "cancelled":
                        statuses = [(await service.require_execution(child.execution_id)).status for child in children]
                        if statuses == [ExecutionStatus.CANCELLED, ExecutionStatus.CANCELLED]:
                            break
            assert not model.release_workers.is_set()
            waiting = await service.require_execution(parent.execution_id)
            assert waiting.status is ExecutionStatus.CANCELLING
            assert waiting.budget_consumed == 0 and waiting.budget_reserved == 0
            assert (await service.sessions.run(parent.session_id, handle.run_id)).receipt is None
            for other in roots[1:]:
                assert (await service.require_execution(other.execution_id)).status is ExecutionStatus.RUNNING
            model.release_parent_cleanup.set()
            async with asyncio.timeout(5):
                receipt = await handle.result()
            assert receipt.state.value == "cancelled"
            assert receipt.usage.model_calls == 1
            assert receipt.usage.output_tokens is None and receipt.usage.budget_output_tokens == parent.budget_limit
            async with asyncio.timeout(5):
                async for event in service.events():
                    if event.subject == parent.execution_id and event.data.get("status") == "cancelled":
                        break
            cancelled = await service.require_execution(parent.execution_id)
            assert cancelled.status is ExecutionStatus.CANCELLED
            assert cancelled.budget_reserved == 0 and cancelled.budget_consumed == cancelled.budget_limit
            child_statuses = [(await service.require_execution(child.execution_id)).status for child in children]
            assert child_statuses == [ExecutionStatus.CANCELLED, ExecutionStatus.CANCELLED]
            assert all(not service.store.execution_runs(child.execution_id) for child in children)
            assert len(service.store.execution_runs(parent.execution_id)) == 1
            assert (
                service.store.execution_runs(parent.execution_id)[0].charged_budget_output_tokens
                == cancelled.budget_limit
            )
            for other in roots[1:]:
                assert (await service.require_execution(other.execution_id)).status is ExecutionStatus.RUNNING
            assert service.ready
        finally:
            model.release_parent_cleanup.set()
            model.release_workers.set()
            await service.shutdown()

    asyncio.run(scenario())


def test_cancelled_parent_receipt_keeps_its_usage_until_owned_children_refund_their_unused_budget(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        model = ParentCancellationModel()
        model.release_parent_cleanup.set()
        service = product_service(tmp_path, model)
        space = service.store.create_space(SpaceConfiguration(name="Cancellation budget"), request_id="space")
        parent = service.store.create_execution(
            ExecutionSpec(space_id=space.space_id, objective="Parent model barrier", budget_limit=1_000),
            request_id="parent",
        )
        item = service.store.seed_execution_input(parent.execution_id)
        await service.deliver_inputs()
        service.store.transition_execution(
            parent.execution_id, ExecutionStatus.RUNNING, expected_revision=parent.revision
        )
        mapping = ExecutionRun(execution_id=parent.execution_id, attempt=1, invocation_id="parent/1")
        service.store.record_execution_run(mapping)
        binding = await service.agents.worker(parent, service.ignore_event, service.delegate, service.progress)
        binding.ready.set()
        handle = await binding.runner.start(
            binding.agent,
            session=Session(parent.session_id, service.sessions),
            invocation_id=mapping.invocation_id,
            input_ids=(item.input_id,),
            input_mode="live",
            limits=RunLimits(max_output_tokens=1_000),
            context=binding.context,
        )
        service.store.record_execution_run(mapping.model_copy(update={"run_id": handle.run_id, "state": "running"}))
        try:
            async with asyncio.timeout(5):
                await model.started["Parent model barrier"].wait()
            children = [
                service.store.create_execution(
                    ExecutionSpec(
                        space_id=space.space_id,
                        parent_execution_id=parent.execution_id,
                        objective=name,
                        budget_limit=100,
                    ),
                    request_id=name,
                )
                for name in ("First queued child", "Second queued child")
            ]
            current = await service.require_execution(parent.execution_id)
            service.store.control_execution(
                parent.execution_id, "cancel", request_id="cancel", expected_revision=current.revision
            )
            outcome = await handle.cancel(reason="user_cancelled")
            assert outcome.usage.budget_output_tokens == 1_000
            await service.finish_work(parent.execution_id, mapping, outcome)
            waiting = await service.require_execution(parent.execution_id)
            assert waiting.status is ExecutionStatus.CANCELLING
            assert waiting.budget_consumed == 0 and waiting.budget_reserved == 200
            receipt = service.store.execution_runs(parent.execution_id)[0]
            assert receipt.state == "cancelled" and receipt.usage == outcome.usage.model_dump(mode="json")
            assert receipt.charged_budget_output_tokens == 0
            await service.finish_cancellation(children[0].execution_id)
            await service.finish_work(parent.execution_id, mapping, outcome)
            waiting = await service.require_execution(parent.execution_id)
            assert waiting.status is ExecutionStatus.CANCELLING
            assert waiting.budget_consumed == 0 and waiting.budget_reserved == 100
            await service.finish_cancellation(children[1].execution_id)
            await service.finish_work(parent.execution_id, mapping, outcome)
            await service.finish_work(parent.execution_id, mapping, outcome)
            finished = await service.require_execution(parent.execution_id)
            assert finished.status is ExecutionStatus.CANCELLED and finished.budget_reserved == 0
            assert finished.budget_consumed == 1_000
            assert len(service.store.execution_runs(parent.execution_id)) == 1
            assert service.store.execution_runs(parent.execution_id)[0].charged_budget_output_tokens == 1_000
            assert len(model.requests) == 1
        finally:
            await binding.runner.aclose()
            await service.agents.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("failure_scope", ["worker", "children"])
def test_provider_http_500_charges_the_committed_sdk_receipt_before_blocking_and_cannot_replay_for_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_scope: str
) -> None:
    async def scenario() -> None:
        requested = asyncio.Event()
        release_error = asyncio.Event()
        requests: list[httpx.Request] = []

        async def respond(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            requested.set()
            await release_error.wait()
            return httpx.Response(500, json={"error": {"message": "provider overloaded", "type": "server_error"}})

        def create_client(
            *, api_key: str, base_url: str | None, timeout: float, max_retries: int, default_headers: Mapping[str, str]
        ) -> AsyncOpenAI:
            return AsyncOpenAI(
                api_key=api_key,
                base_url=base_url,
                timeout=timeout,
                max_retries=max_retries,
                default_headers=default_headers,
                http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
            )

        monkeypatch.setattr(deepseek_provider, "AsyncOpenAI", create_client)
        service = product_service(tmp_path, ProductScenarioModel())
        client = OpenAIModelClient(provider="deepseek", model="test-model", api_key="test-key", max_retries=0)
        service.agents.models = {**service.agents.models, "failure": client}
        space = service.store.create_space(
            SpaceConfiguration(name="Provider failure", model_role="failure"), request_id="space"
        )
        execution = service.store.create_execution(
            ExecutionSpec(space_id=space.space_id, objective="Accepted provider request", budget_limit=1_000),
            request_id="work",
        )
        children = []
        work = asyncio.create_task(service.work(execution.execution_id))
        try:
            async with asyncio.timeout(5):
                await requested.wait()
            handle = service.handles[execution.execution_id]
            if failure_scope == "children":
                children = [
                    service.store.create_execution(
                        ExecutionSpec(
                            space_id=space.space_id,
                            parent_execution_id=execution.execution_id,
                            objective=name,
                            budget_limit=100,
                        ),
                        request_id=name,
                    )
                    for name in ("First queued child", "Second queued child")
                ]
            release_error.set()
            async with asyncio.timeout(5):
                await work
            blocked = await service.require_execution(execution.execution_id)
            assert blocked.status is ExecutionStatus.BLOCKED and blocked.reason == "unhandled_failure"
            mapping = service.store.execution_runs(execution.execution_id)[0]
            assert mapping.run_id is not None
            snapshot = await service.sessions.run(execution.session_id, mapping.run_id)
            receipt = snapshot.receipt
            assert receipt is not None and receipt.state == "failed" and receipt.reason == "system_failure"
            assert receipt.error_code == "model.provider.overloaded"
            assert receipt.usage.budget_output_tokens == 1_000 and receipt.usage.model_calls == 1
            assert receipt.usage.output_tokens is None
            assert mapping.state == "failed" and mapping.usage == receipt.usage.model_dump(mode="json")
            if children:
                assert blocked.budget_reserved == 200 and blocked.budget_consumed == 0
                assert mapping.charged_budget_output_tokens == 0
                for child in children:
                    cancelling = await service.control(
                        child.execution_id, "cancel", request_id=child.execution_id, expected_revision=child.revision
                    )
                    await service.apply_control(cancelling, "cancel")
                await service.reconcile_execution(await service.require_execution(execution.execution_id))
            charged = await service.require_execution(execution.execution_id)
            assert charged.status is ExecutionStatus.BLOCKED
            assert charged.budget_reserved == 0 and charged.budget_consumed == 1_000
            assert service.store.execution_runs(execution.execution_id)[0].charged_budget_output_tokens == 1_000
            with pytest.raises(ModelProviderOverloadedError) as failure:
                await handle.result()
            assert isinstance(failure.value.__cause__, InternalServerError)
            assert failure.value.__cause__.status_code == 500
            assert service.store.reports(execution.execution_id) == ()
            await service.start()
            try:
                restored = await service.require_execution(execution.execution_id)
                assert restored.status is ExecutionStatus.BLOCKED and restored.budget_consumed == 1_000
                await service.control(
                    execution.execution_id, "resume", request_id="resume", expected_revision=restored.revision
                )
                async with asyncio.timeout(5):
                    final = await service.wait(execution.execution_id)
                assert final.status is ExecutionStatus.BLOCKED and final.budget_consumed == 1_000
                assert len(requests) == 1 and len(service.store.execution_runs(execution.execution_id)) == 1
            finally:
                await service.shutdown()
        finally:
            release_error.set()
            if not work.done():
                work.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await work
            if service.owner_descriptor is None:
                await service.agents.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("owned_child", [False, True])
def test_reported_provider_overage_is_charged_in_full_and_settles_the_original_child_grant(
    tmp_path: Path, owned_child: bool
) -> None:
    async def scenario() -> None:
        class ProviderOverageModel(ProductScenarioModel):
            async def stream(self, request: OpenAIRequest | Mapping[str, object]) -> AsyncIterator[OpenAIStreamChunk]:
                assert isinstance(request, OpenAIRequest)
                self.requests.append(request)
                assert request.max_completion_tokens == 1_000
                yield OpenAIStreamChunk(
                    model=self.model,
                    choices=(
                        OpenAIStreamChoice(
                            delta=OpenAIStreamDelta(
                                tool_calls=(
                                    OpenAIStreamToolCall(
                                        index=0,
                                        id="after-provider-overage",
                                        function=OpenAIStreamFunction(name="query_work", arguments="{}"),
                                    ),
                                )
                            ),
                            finish_reason="tool_calls",
                        ),
                    ),
                    usage=OpenAIUsage(prompt_tokens=1, completion_tokens=1_250, total_tokens=1_251),
                )

        model = ProviderOverageModel()
        service = product_service(tmp_path, model)
        space = service.store.create_space(SpaceConfiguration(name="Actual provider usage"), request_id="space")
        parent = (
            service.store.create_execution(
                ExecutionSpec(space_id=space.space_id, objective="Parent grant", budget_limit=1_000),
                request_id="parent",
            )
            if owned_child
            else None
        )
        execution = service.store.create_execution(
            ExecutionSpec(
                space_id=space.space_id,
                parent_execution_id=None if parent is None else parent.execution_id,
                objective="Provider reports more than requested",
                budget_limit=1_000,
            ),
            request_id="work",
        )
        try:
            await service.work(execution.execution_id)
            finished = await service.require_execution(execution.execution_id)
            assert finished.status is ExecutionStatus.FAILED
            assert finished.budget_limit == 1_000 and finished.budget_consumed == 1_250
            mapping = service.store.execution_runs(execution.execution_id)[0]
            assert mapping.run_id is not None and mapping.state == "failed"
            assert mapping.charged_budget_output_tokens == 1_250
            snapshot = await service.sessions.run(execution.session_id, mapping.run_id)
            assert snapshot.receipt is not None and snapshot.receipt.reason == "limit_reached"
            assert snapshot.receipt.usage.output_tokens == snapshot.receipt.usage.budget_output_tokens == 1_250
            assert snapshot.receipt.usage.model_calls == 1
            reports = service.store.reports(execution.execution_id)
            assert len(reports) == 1 and reports[0].kind == "failed"
            assert reports[0].summary == finished.result == "limit_reached"
            service.store.settle_run_usage(execution.execution_id, invocation_id=mapping.invocation_id, consumed=1_250)
            await service.recover()
            assert len(model.requests) == 1
            if parent is not None:
                charged = await service.require_execution(parent.execution_id)
                assert charged.budget_limit == 1_000 and charged.budget_consumed == 1_250
                assert charged.budget_reserved == 0
                reservation = service.store.reserve_budget(parent.execution_id, execution.execution_id, 1_000)
                assert reservation.amount == 1_000 and reservation.consumed == 1_250 and reservation.settled
                assert service.store.settle_budget(reservation.reservation_id, 1_250) == reservation
                with pytest.raises(ProductCapacityError, match="insufficient"):
                    service.store.create_execution(
                        ExecutionSpec(
                            space_id=space.space_id,
                            parent_execution_id=parent.execution_id,
                            objective="Unfunded new grant",
                            budget_limit=1,
                        ),
                        request_id="unfunded",
                    )
                await service.work(parent.execution_id)
                blocked = await service.require_execution(parent.execution_id)
                assert blocked.status is ExecutionStatus.BLOCKED and blocked.reason == "unhandled_failure"
                assert blocked.budget_limit == 1_000 and blocked.budget_consumed == 1_250
                assert service.store.execution_runs(parent.execution_id) == ()
                assert len(model.requests) == 1
            reopened = type(service.store)(service.store.root)
            persisted = reopened.get_execution(execution.execution_id)
            assert persisted is not None and persisted.budget_consumed == 1_250
            assert reopened.execution_runs(execution.execution_id)[0].charged_budget_output_tokens == 1_250
        finally:
            await service.agents.aclose()

    asyncio.run(scenario())
