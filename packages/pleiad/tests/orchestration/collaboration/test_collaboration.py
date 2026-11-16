import asyncio
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path

import pytest

from pleiad.agent.agent import Agent
from pleiad.budgets import BudgetLedger, BudgetStateError
from pleiad.context.budget import ApproximateTokenCounter, ContextBudget
from pleiad.contracts import ToolRef, UsageCharge
from pleiad.events.context import ContextCompactionCompletedEvent, ContextCompactionStartedEvent
from pleiad.messages.images import ImageBlock, ImageReadScope, ImageReference, ImageResolver, ResolvedImage
from pleiad.messages.message import TextBlock
from pleiad.models.errors import ModelConnectionError
from pleiad.models.openai.request import OpenAIRequest
from pleiad.models.openai.response import OpenAIStreamChunk
from pleiad.orchestration.collaboration.backends.in_memory import InMemoryCollaborationStore
from pleiad.orchestration.collaboration.backends.sqlite import SqliteCollaborationStore
from pleiad.orchestration.collaboration.binding import CollaborationHost, WorkAdmission, WorkBinding
from pleiad.orchestration.collaboration.contracts import (
    CloseRequest,
    CollaborationLimits,
    EventCursor,
    InterruptRequest,
    MessageRequest,
    SpawnRequest,
    WorkPermit,
    WorkProposal,
    WorkRejection,
)
from pleiad.orchestration.collaboration.endpoint import MemberEndpoint
from pleiad.orchestration.collaboration.errors import CollaborationConflict, CollaborationStorageError
from pleiad.orchestration.collaboration.group import CollaborationGroup
from pleiad.orchestration.collaboration.state import WorkRecord
from pleiad.orchestration.collaboration.store import CollaborationStore
from pleiad.orchestration.collaboration.tools import collaboration_tools
from pleiad.runtime.checkpoint import ResumeDecision
from pleiad.runtime.limits import RunLimits
from pleiad.runtime.policy import HostPolicy
from pleiad.runtime.result import RunResult, RunSuspension
from pleiad.runtime.runner import Runner
from pleiad.sessions.backends.in_memory import InMemorySessionStore
from pleiad.sessions.backends.sqlite import SqliteSessionStore
from pleiad.sessions.input import InputEnvelope
from pleiad.sessions.model_step import ModelStep
from pleiad.sessions.session import Session
from pleiad.sessions.store import SessionStore
from pleiad.skills.loader import SkillLoader
from pleiad.tools.base import FunctionTool
from pleiad.tools.contracts import ParentRunLimits, ToolContext, ToolDescriptor, ToolResult
from pleiad.tools.registry import ToolRegistry
from pleiad.tools.runtime import ToolRuntime
from tests.fakes.runtime import (
    EchoArguments,
    FakeOpenAIModel,
    RequestMessageCounter,
    interrupting_tool,
    text_batch,
    tool_batch,
)
from tests.runtime.execution.test_multimodal import IMAGE_BYTES, image_reference
from tests.runtime_support import run_agent


class GatedModel(FakeOpenAIModel):
    """Hold provider responses at explicit barriers to exercise admission races."""

    def __init__(self, responses: tuple[str, ...]) -> None:
        super().__init__(tuple(text_batch(value, f"response-{index}") for index, value in enumerate(responses)))
        self.entered: asyncio.Queue[None] = asyncio.Queue()
        self.release = asyncio.Semaphore(0)

    async def stream(self, request: OpenAIRequest | Mapping[str, object]) -> AsyncIterator[OpenAIStreamChunk]:
        await self.entered.put(None)
        await self.release.acquire()
        async for chunk in super().stream(request):
            yield chunk


class GatedCleanupModel(GatedModel):
    """Retain a provider stream until cancellation cleanup is explicitly released."""

    def __init__(self) -> None:
        super().__init__(("unused",))
        self.cleanup_started = asyncio.Event()
        self.finish_cleanup = asyncio.Event()

    async def stream(self, request: OpenAIRequest | Mapping[str, object]) -> AsyncIterator[OpenAIStreamChunk]:
        try:
            async for chunk in super().stream(request):
                yield chunk
        finally:
            self.cleanup_started.set()
            await self.finish_cleanup.wait()


def tool_factory(tool: FunctionTool, descriptor: ToolDescriptor) -> FunctionTool:
    if descriptor != tool.descriptor:
        raise ValueError("Unexpected Tool descriptor")
    return tool


class Harness:
    """Bind real SDK components through a concrete test host and fake model transports."""

    def __init__(
        self,
        models: tuple[FakeOpenAIModel, ...],
        *,
        store: CollaborationStore | None = None,
        sessions: SessionStore | None = None,
        limits: CollaborationLimits | None = None,
        extra_tools: tuple[FunctionTool, ...] = (),
        image_resolver: ImageResolver | None = None,
        close_on_exit: bool = True,
    ) -> None:
        self.models = iter(models)
        self.bindings: dict[str, WorkBinding] = {}
        self.admissions: list[str] = []
        self.revoked = False
        self.run_limits = RunLimits(max_model_steps=4, max_output_tokens=128)
        self.extra_tools = extra_tools
        self.image_resolver = image_resolver
        self.sessions = InMemorySessionStore() if sessions is None else sessions
        self.store = InMemoryCollaborationStore() if store is None else store
        self.group = CollaborationGroup(
            "test-scope",
            root_session=Session("root-session", self.sessions),
            sessions=self.sessions,
            store=self.store,
            host=CollaborationHost(self.admit, self.rebind),
            budgets=BudgetLedger(),
            limits=limits,
            close_on_exit=close_on_exit,
        )

    async def admit(self, proposal: WorkProposal, endpoint: MemberEndpoint) -> WorkAdmission:
        self.admissions.append(proposal.work_id)
        if endpoint.member_id not in self.bindings:
            model = next(self.models)
            registry = ToolRegistry()
            tools = (*collaboration_tools(endpoint), *self.extra_tools)
            for tool in tools:
                registry.register(tool.descriptor, partial(tool_factory, tool))
            runner = Runner(
                tools=registry,
                skills=SkillLoader(),
                tool_runtime=ToolRuntime(),
                image_resolver=self.image_resolver,
                token_counter=ApproximateTokenCounter(image_token_estimator=lambda image: image.width * image.height),
            )
            agent = Agent(
                name="worker",
                model=model,
                prompt="Complete the task.",
                tools=tuple(ToolRef(tool.descriptor.code) for tool in tools),
            )
            self.bindings[endpoint.member_id] = WorkBinding(agent, runner, "worker-v1")
        binding = self.bindings[endpoint.member_id]
        return WorkAdmission(
            WorkPermit(
                authorization_ref=proposal.work_id,
                binding_key=binding.binding_key,
                limits=self.run_limits,
            ),
            binding,
        )

    async def rebind(
        self, proposal: WorkProposal, permit: WorkPermit, endpoint: MemberEndpoint
    ) -> WorkBinding | WorkRejection:
        if self.revoked:
            return WorkRejection(code="revoked", message="Host authorization was revoked.")
        if permit.authorization_ref != proposal.work_id:
            raise ValueError("Permit identity changed")
        return self.bindings[endpoint.member_id]


def authority() -> ToolContext:
    return ToolContext(
        call_id="host-command",
        run_id="root-run",
        parent_limits=ParentRunLimits(
            deadline=datetime.now(UTC) + timedelta(minutes=1),
            remaining_model_steps=64,
            remaining_output_tokens=8192,
            tool_concurrency=4,
        ),
    )


def envelope(text: str) -> InputEnvelope:
    return InputEnvelope(input_id="host-input", kind="user", source="host", content=text)


def spawn_request(key: str = "spawn") -> SpawnRequest:
    return SpawnRequest(request_id=key, profile_id="worker", title=key, content=envelope(key))


async def terminal(endpoint: MemberEndpoint, member_id: str, work_id: str) -> WorkRecord:
    cursor = EventCursor(scope_id=endpoint.group.state.scope_id)
    async with asyncio.timeout(5):
        while True:
            work = next(value for value in await endpoint.status(member_id) if value.proposal.work_id == work_id)
            if work.phase == "terminal":
                return work
            page = await endpoint.wait((member_id,), after=cursor, timeout_seconds=1)
            cursor = page.cursor


@pytest.mark.parametrize("persistent", [False, True])
def test_same_profile_members_run_in_parallel_with_independent_sessions(tmp_path: Path, persistent: bool) -> None:
    async def scenario() -> None:
        first, second = GatedModel(("one",)), GatedModel(("two",))
        harness = Harness(
            (first, second),
            store=SqliteCollaborationStore(tmp_path / "collaboration.sqlite3") if persistent else None,
            sessions=SqliteSessionStore(tmp_path / "sessions.sqlite3") if persistent else None,
        )
        async with harness.group as group:
            await group.root.open_run("root-run")
            a = await group.root.spawn(spawn_request("one"), authority())
            b = await group.root.spawn(spawn_request("two"), authority())
            async with asyncio.timeout(3):
                await first.entered.get()
                await second.entered.get()
            assert a.member_id != b.member_id
            assert group.session(a.member_id).session_id != group.session(b.member_id).session_id
            first.release.release()
            second.release.release()
            assert a.work_id is not None and b.work_id is not None
            a_result = await terminal(group.root, a.member_id, a.work_id)
            b_result = await terminal(group.root, b.member_id, b.work_id)
            assert a_result.receipt is not None and a_result.receipt.final_output == "one"
            assert b_result.receipt is not None and b_result.receipt.final_output == "two"
            assert len(harness.admissions) == 2
            assert len(group.budgets.usage_charges("root-run")) == 2

    asyncio.run(scenario())


def test_active_followup_keeps_original_run_and_does_not_reserve_again() -> None:
    async def scenario() -> None:
        model = GatedModel(("first answer", "revised answer"))
        harness = Harness((model,))
        async with harness.group as group:
            await group.root.open_run("root-run")
            original = await group.root.spawn(spawn_request(), authority())
            await model.entered.get()
            request = MessageRequest(
                request_id="follow", member_id=original.member_id, content=envelope("Please revise")
            )
            follow = await group.root.followup(request, authority())
            assert follow.route == "active_run" and follow.work_id == original.work_id
            assert len(harness.admissions) == 1 and len(group.budgets.reservations) == 1
            model.release.release()
            await model.entered.get()
            model.release.release()
            assert original.work_id is not None
            work = await terminal(group.root, original.member_id, original.work_id)
            assert work.run_id == follow.run_id
            assert work.receipt is not None and work.receipt.final_output == "revised answer"
            assert follow == await group.root.followup(request, authority())
            journal = await group.session(original.member_id).store.snapshot(
                group.session(original.member_id).session_id
            )
            delivered = next(value for value in journal.inputs if value.envelope.input_id == follow.message_id)
            assert delivered.run_id == follow.run_id and delivered.step_id is not None

    asyncio.run(scenario())


def test_idle_followup_reuses_session_and_starts_a_new_invocation() -> None:
    async def scenario() -> None:
        harness = Harness((FakeOpenAIModel((text_batch("one", "a"), text_batch("two", "b"))),))
        async with harness.group as group:
            await group.root.open_run("root-run")
            first = await group.root.spawn(spawn_request(), authority())
            assert first.work_id is not None
            a = await terminal(group.root, first.member_id, first.work_id)
            second = await group.root.followup(
                MessageRequest(request_id="follow", member_id=first.member_id, content=envelope("Continue")),
                authority(),
            )
            assert second.work_id is not None
            b = await terminal(group.root, first.member_id, second.work_id)
            assert second.route == "new_work" and a.run_id != b.run_id
            assert a.proposal.session_id == b.proposal.session_id
            assert len(await group.session(first.member_id).history()) == 2

    asyncio.run(scenario())


@pytest.mark.parametrize("persistent", [False, True], ids=["memory", "sqlite"])
def test_child_compaction_progress_keeps_typed_metadata_and_deduplicates_replay(
    tmp_path: Path, persistent: bool
) -> None:
    async def scenario() -> None:
        database = tmp_path / "collaboration.sqlite3"
        sessions = SqliteSessionStore(tmp_path / "sessions.sqlite3") if persistent else InMemorySessionStore()
        store = SqliteCollaborationStore(database) if persistent else InMemoryCollaborationStore()
        model = FakeOpenAIModel(tuple(text_batch(f"answer-{index}", f"response-{index}") for index in range(4)))
        harness = Harness((model,), sessions=sessions, store=store)
        async with harness.group as group:
            await group.root.open_run("root-run")
            first = await group.root.spawn(spawn_request(), authority())
            assert first.work_id is not None
            await terminal(group.root, first.member_id, first.work_id)
            for index in range(2):
                follow = await group.root.followup(
                    MessageRequest(request_id=f"seed-{index}", member_id=first.member_id, content=envelope("next")),
                    authority(),
                )
                assert follow.work_id is not None
                await terminal(group.root, first.member_id, follow.work_id)
            binding = harness.bindings[first.member_id]
            binding.runner.token_counter = RequestMessageCounter()

            async def summarize(previous: str | None, steps: tuple[ModelStep, ...]) -> str:
                assert previous is None and len(steps) == 2
                return "Earlier questions were answered."

            binding.runner.history_summarizer = summarize
            harness.run_limits = RunLimits(
                max_model_steps=4,
                max_output_tokens=128,
                context_budget=ContextBudget(window_tokens=40, output_reserve=2, compact_at=5, recent_tokens=3),
            )
            follow = await group.root.followup(
                MessageRequest(request_id="compact", member_id=first.member_id, content=envelope("next")), authority()
            )
            assert follow.work_id is not None
            work = await terminal(group.root, first.member_id, follow.work_id)
            assert work.run_id is not None and work.receipt is not None and work.receipt.state == "succeeded"
            page = await group.root.wait(
                (first.member_id,), after=EventCursor(scope_id=group.state.scope_id), timeout_seconds=0
            )
            projected = tuple(event for event in page.events if event.compaction is not None)
            assert len(projected) == 2
            started, completed = projected
            assert isinstance(started.compaction, ContextCompactionStartedEvent)
            assert isinstance(completed.compaction, ContextCompactionCompletedEvent)
            assert started.compaction.compaction_id == completed.compaction.compaction_id
            assert started.kind == "run.context.compaction.started"
            assert completed.kind == "run.context.compaction.completed"
            assert completed.run_id == completed.compaction.run_id == work.run_id
            assert completed.compaction.parent_run_id == "root-run"
            assert completed.compaction.agent_name == "worker"
            assert completed.step_id == completed.compaction.step_id
            assert completed.compaction.summary is not None
            assert [step.sequence for step in completed.compaction.summary.added_steps] == [1, 2]
            original = await sessions.read_events(work.proposal.session_id, work.run_id)
            source = next(record for record in original if record.sequence == completed.source_sequence)
            assert completed.compaction.model_dump(mode="json") == source.data
            before = group.state.events
            await group.replay_progress(follow.work_id, work.run_id)
            assert group.state.events == before
            restored = SqliteCollaborationStore(database) if persistent else store
            snapshot = await restored.read(group.state.scope_id)
            assert snapshot is not None
            assert tuple(event for event in snapshot.events if event.compaction is not None) == projected
            assert len(model.requests) == 4

    asyncio.run(scenario())


def test_message_does_not_reopen_final_model_step_or_wake_idle_member() -> None:
    async def scenario() -> None:
        model = GatedModel(("done",))
        harness = Harness((model,))
        async with harness.group as group:
            await group.root.open_run("root-run")
            first = await group.root.spawn(spawn_request(), authority())
            await model.entered.get()
            sent = await group.root.send_message(
                MessageRequest(request_id="message", member_id=first.member_id, content=envelope("FYI")), authority()
            )
            model.release.release()
            assert first.work_id is not None
            await terminal(group.root, first.member_id, first.work_id)
            assert len(model.requests) == 1
            pending = await group.session(first.member_id).pending_inputs()
            assert any(value.envelope.input_id == sent.message_id for value in pending)
            await group.root.send_message(
                MessageRequest(request_id="idle-message", member_id=first.member_id, content=envelope("later")),
                authority(),
            )
            assert len(harness.admissions) == 1

    asyncio.run(scenario())


def test_conflicting_retry_does_not_create_a_second_member() -> None:
    async def scenario() -> None:
        harness = Harness((FakeOpenAIModel((text_batch("done", "a"),)),))
        async with harness.group as group:
            await group.root.open_run("root-run")
            request = spawn_request()
            first = await group.root.spawn(request, authority())
            assert first == await group.root.spawn(request, authority())
            with pytest.raises(CollaborationConflict, match="different command payload"):
                await group.root.spawn(request.model_copy(update={"title": "changed"}), authority())
            assert len(await group.root.list_members()) == 2

    asyncio.run(scenario())


def test_interrupt_requires_exact_run_and_settles_before_followup() -> None:
    async def scenario() -> None:
        model = GatedModel(("after interrupt",))
        harness = Harness((model,))
        async with harness.group as group:
            await group.root.open_run("root-run")
            first = await group.root.spawn(spawn_request(), authority())
            await model.entered.get()
            assert first.work_id is not None
            work = (await group.root.status(first.member_id))[0]
            assert work.run_id is not None
            wrong = await group.root.interrupt(
                InterruptRequest(
                    request_id="wrong",
                    member_id=first.member_id,
                    expected_work_id=first.work_id,
                    expected_run_id="wrong",
                ),
                authority(),
            )
            assert not wrong.accepted
            stopped = await group.root.interrupt(
                InterruptRequest(
                    request_id="stop",
                    member_id=first.member_id,
                    expected_work_id=first.work_id,
                    expected_run_id=work.run_id,
                ),
                authority(),
            )
            assert stopped.accepted
            result = await group.root.read_result(first.work_id)
            assert result is not None and result.state == "cancelled"
            follow = await group.root.followup(
                MessageRequest(request_id="next", member_id=first.member_id, content=envelope("new task")), authority()
            )
            await model.entered.get()
            model.release.release()
            assert follow.work_id is not None
            finished = await terminal(group.root, follow.member_id, follow.work_id)
            assert finished.run_id != work.run_id and finished.receipt is not None
            assert finished.receipt.final_output == "after interrupt"

    asyncio.run(scenario())


def test_wait_cursors_are_readonly_and_results_remain_available_after_close() -> None:
    async def scenario() -> None:
        harness = Harness((FakeOpenAIModel((text_batch("done", "a"),)),))
        async with harness.group as group:
            await group.root.open_run("root-run")
            first = await group.root.spawn(spawn_request(), authority())
            assert first.work_id is not None
            await terminal(group.root, first.member_id, first.work_id)
            cursor = EventCursor(scope_id=group.state.scope_id)
            a = await group.root.wait((first.member_id,), after=cursor, timeout_seconds=0)
            b = await group.root.wait((first.member_id,), after=cursor, timeout_seconds=0)
            assert a == b
            snapshot = await harness.store.read(group.state.scope_id)
            timed_out = await group.root.wait((first.member_id,), after=a.cursor, timeout_seconds=0)
            assert timed_out.timed_out
            assert snapshot == await harness.store.read(group.state.scope_id)
            await group.root.close_member(CloseRequest(request_id="close", member_id=first.member_id), authority())
            result = await group.root.read_result(first.work_id)
            assert result is not None and result.final_output == "done"

    asyncio.run(scenario())


def test_queue_is_bounded_and_closed_members_never_start_queued_work() -> None:
    async def scenario() -> None:
        model = GatedModel(("first", "queued"))
        harness = Harness((model,), limits=CollaborationLimits(max_active_runs=1, max_queued_work=1))
        async with harness.group as group:
            await group.root.open_run("root-run")
            first = await group.root.spawn(spawn_request(), authority())
            await model.entered.get()
            active = (await group.root.status(first.member_id))[0]
            assert active.run_id is not None and first.work_id is not None
            await group.endpoint(first.member_id).before_finish(active.run_id)
            queued = await group.root.followup(
                MessageRequest(request_id="queued", member_id=first.member_id, content=envelope("later")), authority()
            )
            rejected = await group.root.followup(
                MessageRequest(request_id="overflow", member_id=first.member_id, content=envelope("overflow")),
                authority(),
            )
            assert queued.accepted and queued.route == "new_work"
            assert not rejected.accepted and rejected.code == "queue_full"
            await group.root.close_member(CloseRequest(request_id="close", member_id=first.member_id), authority())
            assert all(work.phase == "terminal" for work in await group.root.status(first.member_id))
            assert len(model.requests) == 0

    asyncio.run(scenario())


def test_cancelled_active_followup_is_never_replayed_into_the_next_run() -> None:
    async def scenario() -> None:
        model = GatedModel(("new answer",))
        harness = Harness((model,))
        async with harness.group as group:
            await group.root.open_run("root-run")
            first = await group.root.spawn(spawn_request(), authority())
            await model.entered.get()
            follow = await group.root.followup(
                MessageRequest(request_id="old-follow", member_id=first.member_id, content=envelope("never replay me")),
                authority(),
            )
            assert first.work_id is not None and follow.run_id is not None
            await group.root.interrupt(
                InterruptRequest(
                    request_id="stop",
                    member_id=first.member_id,
                    expected_work_id=first.work_id,
                    expected_run_id=follow.run_id,
                ),
                authority(),
            )
            fresh = await group.root.followup(
                MessageRequest(request_id="fresh", member_id=first.member_id, content=envelope("fresh task")),
                authority(),
            )
            await model.entered.get()
            model.release.release()
            assert fresh.work_id is not None
            await terminal(group.root, fresh.member_id, fresh.work_id)
            assert "never replay me" not in model.requests[-1].model_dump_json()
            assert follow.message_id is not None
            status = await group.root.message_status(follow.message_id)
            assert status.step_id is None and status.state == "revoked"

    asyncio.run(scenario())


def test_two_followups_after_the_final_gate_get_distinct_serial_invocations() -> None:
    async def scenario() -> None:
        model = GatedModel(("first", "second", "third"))
        harness = Harness((model,))
        async with harness.group as group:
            await group.root.open_run("root-run")
            original = await group.root.spawn(spawn_request(), authority())
            await model.entered.get()
            work = (await group.root.status(original.member_id))[0]
            assert work.run_id is not None
            assert not await group.endpoint(original.member_id).before_finish(work.run_id)
            a, b = await asyncio.gather(
                *(
                    group.root.followup(
                        MessageRequest(request_id=key, member_id=original.member_id, content=envelope(key)), authority()
                    )
                    for key in ("second", "third")
                )
            )
            assert a.route == b.route == "new_work" and a.work_id != b.work_id
            for receipt in (original, a, b):
                model.release.release()
                assert receipt.work_id is not None
                await terminal(group.root, receipt.member_id, receipt.work_id)
                if receipt is not b:
                    await model.entered.get()
            works = await group.root.status(original.member_id)
            assert len({item.run_id for item in works}) == 3

    asyncio.run(scenario())


class UnavailableModel(FakeOpenAIModel):
    async def stream(self, request: OpenAIRequest | Mapping[str, object]) -> AsyncIterator[OpenAIStreamChunk]:
        if not isinstance(request, OpenAIRequest):
            raise TypeError("Expected a parsed provider request")
        self.requests.append(request)
        yield OpenAIStreamChunk(id="failed", model=self.model, choices=())
        raise ModelConnectionError("Transport unavailable")


def test_provider_failure_does_not_cancel_an_unrelated_sibling() -> None:
    async def scenario() -> None:
        sibling = GatedModel(("survived",))
        harness = Harness((UnavailableModel(()), sibling))
        async with harness.group as group:
            await group.root.open_run("root-run")
            failed = await group.root.spawn(spawn_request("failed"), authority())
            other = await group.root.spawn(spawn_request("other"), authority())
            await sibling.entered.get()
            assert failed.work_id is not None and other.work_id is not None
            result = await terminal(group.root, failed.member_id, failed.work_id)
            assert result.receipt is not None and result.receipt.state == "failed"
            sibling.release.release()
            successful = await terminal(group.root, other.member_id, other.work_id)
            assert successful.receipt is not None and successful.receipt.final_output == "survived"

    asyncio.run(scenario())


def test_nested_spawn_rejects_capacity_instead_of_queuing_behind_its_ancestor() -> None:
    async def scenario() -> None:
        model = GatedModel(("done",))
        harness = Harness((model,), limits=CollaborationLimits(max_active_runs=1))
        async with harness.group as group:
            await group.root.open_run("root-run")
            parent = await group.root.spawn(spawn_request(), authority())
            await model.entered.get()
            work = (await group.root.status(parent.member_id))[0]
            assert work.run_id is not None
            response = await group.endpoint(parent.member_id).spawn(
                spawn_request("nested"), authority().model_copy(update={"run_id": work.run_id})
            )
            assert not response.accepted and response.code == "nested_capacity_exhausted"
            assert len(harness.admissions) == 1
            model.release.release()

    asyncio.run(scenario())


def test_scope_close_cancels_all_owned_handles_and_preserves_borrowed_runners() -> None:
    async def scenario() -> None:
        models = (GatedModel(("unused",)), GatedModel(("unused",)))
        harness = Harness(models)
        async with harness.group as group:
            await group.root.open_run("root-run")
            for key in ("one", "two"):
                await group.root.spawn(spawn_request(key), authority())
            for model in models:
                await model.entered.get()
        assert all(task.done() for task in group.tasks.values())
        assert all(handle.finished for handle in group.handles.values())
        assert not group.budgets.reservations
        assert group.state.state == "closed"
        assert all(
            work.receipt is not None and work.receipt.state == "cancelled" for work in group.state.works.values()
        )

    asyncio.run(scenario())


@pytest.mark.parametrize("persistent", [False, True])
def test_deployment_exit_preserves_member_session_for_recovered_followup(tmp_path: Path, persistent: bool) -> None:
    async def scenario() -> None:
        store = SqliteCollaborationStore(tmp_path / "collaboration.sqlite3") if persistent else None
        sessions = SqliteSessionStore(tmp_path / "sessions.sqlite3") if persistent else None
        original_model = GatedModel(("must not replay",))
        live = Harness((original_model,), store=store, sessions=sessions, close_on_exit=False)
        async with live.group as group:
            await group.root.open_run("root-run")
            original = await group.root.spawn(spawn_request(), authority())
            await original_model.entered.get()
            original_session = group.session(original.member_id).session_id
        assert original.work_id is not None
        result = await group.root.read_result(original.work_id)
        assert result is not None and result.state == "cancelled"
        assert all(handle.finished for handle in group.handles.values())
        assert all(task.done() for task in group.tasks.values())
        assert not group.budgets.reservations
        assert group.state.state == "open"
        assert group.state.members[original.member_id].state == "open"
        with pytest.raises(CollaborationConflict, match="active owner"):
            await group.root.open_run("must-not-run")
        with pytest.raises(CollaborationConflict, match="not accepting"):
            await group.root.followup(
                MessageRequest(request_id="detached", member_id=original.member_id, content=envelope("reject")),
                authority(),
            )
        with pytest.raises(CollaborationConflict, match="recovery"):
            await group.__aenter__()
        replacement_model = FakeOpenAIModel((text_batch("continued", "new-response"),))
        recovered = Harness(
            (replacement_model,),
            store=SqliteCollaborationStore(tmp_path / "collaboration.sqlite3") if persistent else live.store,
            sessions=SqliteSessionStore(tmp_path / "sessions.sqlite3") if persistent else live.sessions,
            close_on_exit=False,
        )
        await recovered.group.recover(expected_epoch=1)
        assert not replacement_model.requests and not recovered.admissions
        async with recovered.group as group:
            await group.root.open_run("next-root-run")
            followup = await group.root.followup(
                MessageRequest(request_id="continue", member_id=original.member_id, content=envelope("continue")),
                authority().model_copy(update={"run_id": "next-root-run"}),
            )
            assert followup.accepted and followup.route == "new_work"
            assert followup.work_id is not None and followup.work_id != original.work_id
            assert group.session(followup.member_id).session_id == original_session
            work = await terminal(group.root, followup.member_id, followup.work_id)
            assert work.receipt is not None and work.receipt.final_output == "continued"
            assert len(replacement_model.requests) == 1
            assert await group.root.read_result(original.work_id) == result

    asyncio.run(scenario())


def test_cancelled_deployment_owner_settles_children_without_closing_members() -> None:
    async def scenario() -> None:
        model = GatedCleanupModel()
        harness = Harness((model,), close_on_exit=False)

        async def deployment() -> None:
            async with harness.group as group:
                await group.root.open_run("root-run")
                await group.root.spawn(spawn_request(), authority())
                await asyncio.Event().wait()

        async with asyncio.TaskGroup() as owners:
            owner = owners.create_task(deployment())
            await model.entered.get()
            owner.cancel()
            await model.cleanup_started.wait()
            owner.cancel()
            model.finish_cleanup.set()
            with pytest.raises(asyncio.CancelledError):
                await owner
        saved = await harness.store.read("test-scope")
        assert saved is not None and saved.state == "open"
        assert all(member.state == "open" for member in saved.members.values())
        assert all(work.receipt is not None and work.receipt.state == "cancelled" for work in saved.works.values())
        assert all(task.done() for task in harness.group.tasks.values())
        assert not harness.group.entered and not harness.group.budgets.reservations

    asyncio.run(scenario())


def test_deployment_exit_keeps_unknown_work_blocked_without_replaying_or_refunding() -> None:
    async def scenario() -> None:
        model = GatedModel(("unused",))
        live = Harness((model,))
        replica = InMemoryCollaborationStore()
        sessions = InMemorySessionStore()
        async with live.group as group:
            await group.root.open_run("root-run")
            original = await group.root.spawn(spawn_request(), authority())
            await model.entered.get()
            async with group.lock:
                await replica.write(group.state.model_copy(update={"revision": 1}))
                session = group.session(original.member_id)
                journal = await session.store.snapshot(session.session_id)
                await sessions.transact(session.session_id, lambda state: (journal, None))
        recovered = Harness((), store=replica, sessions=sessions, close_on_exit=False)
        await recovered.group.recover(expected_epoch=1)
        charges = recovered.group.budgets.usage_charges("root-run")
        assert charges and charges[0].output_tokens > 0
        async with recovered.group as group:
            await group.root.open_run("root-run")
        saved = await replica.read("test-scope")
        assert saved is not None and original.work_id is not None
        assert saved.state == "open" and saved.members[original.member_id].state == "open"
        work = saved.works[original.work_id]
        assert work.resolution == "unknown" and work.receipt is None
        assert recovered.group.budgets.usage_charges("root-run") == charges
        assert not model.requests and not recovered.admissions
        next_owner = Harness((), store=replica, sessions=sessions, close_on_exit=False)
        await next_owner.group.recover(expected_epoch=2)
        async with next_owner.group as group:
            await group.root.open_run("root-run")
            rejected = await group.root.followup(
                MessageRequest(request_id="retry", member_id=original.member_id, content=envelope("no replay")),
                authority(),
            )
            assert not rejected.accepted and rejected.code == "reconciliation_required"
            assert not next_owner.admissions

    asyncio.run(scenario())


def test_explicit_close_still_terminates_a_preserved_deployment_scope() -> None:
    async def scenario() -> None:
        harness = Harness((GatedModel(("unused",)),), close_on_exit=False)
        async with harness.group as group:
            await group.root.open_run("root-run")
            await group.root.spawn(spawn_request(), authority())
            await group.close()
        saved = await harness.store.read("test-scope")
        assert saved is not None and saved.state == "closed"
        assert all(member.state == "closed" for member in saved.members.values())
        recovered = Harness((), store=harness.store, sessions=harness.sessions, close_on_exit=False)
        await recovered.group.recover(expected_epoch=1)
        with pytest.raises(CollaborationConflict, match="closed"):
            await recovered.group.__aenter__()

    asyncio.run(scenario())


def test_paused_member_rejects_followup_and_resumes_the_original_checkpoint() -> None:
    async def scenario() -> None:
        tool = FunctionTool(
            ToolDescriptor(name="echo", code="echo", description="Request host approval"),
            interrupting_tool,
            EchoArguments,
        )
        model = FakeOpenAIModel((tool_batch("call-1"), text_batch("approved", "after-approval")))
        harness = Harness((model,), extra_tools=(tool,))
        async with harness.group as group:
            await group.root.open_run("root-run")
            original = await group.root.spawn(spawn_request(), authority())
            assert original.work_id is not None
            cursor = EventCursor(scope_id=group.state.scope_id)
            async with asyncio.timeout(5):
                while (await group.root.status(original.member_id))[0].phase != "paused":
                    cursor = (await group.root.wait((original.member_id,), after=cursor, timeout_seconds=1)).cursor
            work = (await group.root.status(original.member_id))[0]
            assert work.run_id is not None
            rejected = await group.root.followup(
                MessageRequest(request_id="paused-follow", member_id=original.member_id, content=envelope("do more")),
                authority(),
            )
            assert not rejected.accepted and rejected.code == "run_paused"
            checkpoint = await group.session(original.member_id).checkpoint(work.run_id)
            assert checkpoint is not None
            now = datetime.now(UTC)
            decision = ResumeDecision(
                interruption_id=checkpoint.interruptions[0].interruption_id,
                result=ToolResult(
                    call_id="call-1", ok=True, output={"approved": True}, started_at=now, finished_at=now
                ),
            )
            await group.root.resume_work(original.work_id, authority(), decisions=(decision,))
            result = await terminal(group.root, original.member_id, original.work_id)
            assert result.run_id == work.run_id
            assert result.receipt is not None and result.receipt.final_output == "approved"

    asyncio.run(scenario())


def test_multimodal_child_uses_real_resolver_on_each_tool_step() -> None:
    async def scenario() -> None:
        reads: list[ImageReadScope] = []

        async def resolver(reference: ImageReference, scope: ImageReadScope) -> ResolvedImage:
            reads.append(scope)
            return ResolvedImage(reference, IMAGE_BYTES)

        model = FakeOpenAIModel(
            (tool_batch("list-1", name="list_agents", arguments="{}"), text_batch("image read", "done"))
        )
        harness = Harness((model,), image_resolver=resolver)
        async with harness.group as group:
            await group.root.open_run("root-run")
            request = spawn_request().model_copy(
                update={
                    "content": InputEnvelope(
                        input_id="image",
                        kind="user",
                        source="host",
                        content=(TextBlock(text="inspect"), ImageBlock(image=image_reference())),
                    )
                }
            )
            accepted = await group.root.spawn(request, authority())
            assert accepted.work_id is not None
            result = await terminal(group.root, accepted.member_id, accepted.work_id)
            assert result.receipt is not None and result.receipt.final_output == "image read"
            assert len(reads) == 2 and all(value.session_id == result.proposal.session_id for value in reads)
            assert all("data:image/png;base64," in request.model_dump_json() for request in model.requests)

    asyncio.run(scenario())


def test_model_can_dynamically_create_two_members_through_bound_tools() -> None:
    async def scenario() -> None:
        harness = Harness(tuple(FakeOpenAIModel((text_batch(value, value),)) for value in ("one", "two")))
        async with harness.group as group:
            model = FakeOpenAIModel(
                (
                    tool_batch("spawn-1", name="spawn_agent", arguments=spawn_request("one").model_dump_json()),
                    tool_batch("spawn-2", name="spawn_agent", arguments=spawn_request("two").model_dump_json()),
                    tool_batch("list", name="list_agents", arguments="{}"),
                    text_batch("root done", "done"),
                )
            )
            registry = ToolRegistry()
            tools = collaboration_tools(group.root)
            for tool in tools:
                registry.register(tool.descriptor, partial(tool_factory, tool))
            runner = Runner(tools=registry, skills=SkillLoader(), tool_runtime=ToolRuntime())
            agent = Agent(
                name="root",
                model=model,
                prompt="Delegate both tasks.",
                tools=tuple(ToolRef(value.descriptor.code) for value in tools),
            )
            result = await run_agent(runner, agent, "coordinate", session=group.root_session)
            assert isinstance(result, RunResult) and result.final_output == "root done"
            members = await group.root.list_members()
            assert len(members) == 3 and len({value.session_id for value in members}) == 3
            assert all(value.phase == "terminal" for value in group.state.works.values())

    asyncio.run(scenario())


def test_recovery_preserves_unknown_work_without_resubmitting_the_model() -> None:
    async def scenario() -> None:
        model = GatedModel(("unused",))
        live = Harness((model,))
        recovered_store = InMemoryCollaborationStore()
        recovered_sessions = InMemorySessionStore()
        async with live.group as group:
            await group.root.open_run("root-run")
            accepted = await group.root.spawn(spawn_request(), authority())
            await model.entered.get()
            async with group.lock:
                await recovered_store.write(group.state.model_copy(update={"revision": 1}))
                session = group.session(accepted.member_id)
                journal = await session.store.snapshot(session.session_id)
                await recovered_sessions.transact(session.session_id, lambda state: (journal, None))
            assert accepted.work_id is not None
        recovered = Harness((), store=recovered_store, sessions=recovered_sessions)
        await recovered.group.recover(expected_epoch=1)
        async with recovered.group as group:
            await group.root.open_run("root-run")
            work = (await group.root.status(accepted.member_id))[0]
            assert work.resolution == "unknown" and work.run_id is not None
            assert group.state.owner_epoch == 2
            rejected = await group.root.followup(
                MessageRequest(request_id="retry", member_id=accepted.member_id, content=envelope("do not replay")),
                authority(),
            )
            assert not rejected.accepted and rejected.code == "reconciliation_required"
            assert not recovered.admissions and not model.requests
            snapshot = await group.session(accepted.member_id).store.run(work.proposal.session_id, work.run_id)
            await group.reconcile_work(
                accepted.work_id,
                runner=live.bindings[accepted.member_id].runner,
                request_id="reconcile",
                expected_epoch=snapshot.writer_epoch,
                expected_revision=snapshot.revision,
            )
            reconciled = await group.root.read_result(accepted.work_id)
            assert reconciled is not None and reconciled.reason == "interrupted"

    asyncio.run(scenario())


@pytest.mark.parametrize("persistent", [False, True])
def test_recovered_scope_fences_old_writers_and_returns_original_command_receipts(
    tmp_path: Path, persistent: bool
) -> None:
    async def scenario() -> None:
        store = (
            SqliteCollaborationStore(tmp_path / "collaboration.sqlite3") if persistent else InMemoryCollaborationStore()
        )
        harness = Harness((FakeOpenAIModel((text_batch("done", "a"),)),), store=store)
        async with harness.group as group:
            await group.root.open_run("root-run")
            receipt = await group.root.spawn(spawn_request(), authority())
            assert receipt.work_id is not None
            await terminal(group.root, receipt.member_id, receipt.work_id)
        saved = await store.read("test-scope")
        assert saved is not None
        reopened = Harness((), store=store, sessions=harness.sessions)
        await reopened.group.recover(expected_epoch=1)
        result = await reopened.group.root.read_result(receipt.work_id)
        assert result is not None and result.final_output == "done"
        assert receipt == await reopened.group.root.spawn(spawn_request(), authority())
        with pytest.raises(CollaborationStorageError, match="stale"):
            await store.write(saved.model_copy(update={"revision": reopened.group.state.revision + 1}))

    asyncio.run(scenario())


def test_shared_ledger_recovery_preserves_other_modes_reservations_and_unknown_charges() -> None:
    ledger = BudgetLedger()
    parent = authority().parent_limits
    assert parent is not None
    assert ledger.reserve("old-subagent", "root-run", parent, 4, 128) is not None
    assert ledger.reserve("collaboration-work", "root-run", parent, 4, 128) is not None
    restored = BudgetLedger()
    restored.import_snapshot(ledger.export())
    restored.settle("collaboration-work", None)
    before = restored.export()
    restored.settle("collaboration-work", None)
    assert before == restored.export()
    assert "old-subagent" in restored.reservations
    assert restored.usage_charges("root-run") == (
        UsageCharge(parent_run_id="root-run", charge_id="collaboration-work", model_steps=4, output_tokens=128),
    )
    with pytest.raises(BudgetStateError, match="different live ledger"):
        restored.import_snapshot(ledger.export())


def test_host_revocation_before_dispatch_stops_work_without_a_model_charge() -> None:
    async def scenario() -> None:
        model = FakeOpenAIModel((text_batch("must not run", "a"),))
        harness = Harness((model,))
        harness.revoked = True
        async with harness.group as group:
            await group.root.open_run("root-run")
            receipt = await group.root.spawn(spawn_request(), authority())
            assert receipt.work_id is not None
            work = await terminal(group.root, receipt.member_id, receipt.work_id)
            assert work.reason == "revoked" and work.run_id is None
            assert not model.requests
            charge = group.budgets.usage_charges("root-run")[0]
            assert charge.model_steps == charge.output_tokens == 0

    asyncio.run(scenario())


def test_inbox_limit_counts_delivered_but_unconsumed_messages() -> None:
    async def scenario() -> None:
        model = GatedModel(("done",))
        harness = Harness((model,), limits=CollaborationLimits(max_pending_messages=1))
        async with harness.group as group:
            await group.root.open_run("root-run")
            receipt = await group.root.spawn(spawn_request(), authority())
            await model.entered.get()
            await group.root.send_message(
                MessageRequest(request_id="message-1", member_id=receipt.member_id, content=envelope("pending")),
                authority(),
            )
            with pytest.raises(CollaborationConflict, match="inbox is full"):
                await group.root.send_message(
                    MessageRequest(request_id="message-2", member_id=receipt.member_id, content=envelope("overflow")),
                    authority(),
                )
            model.release.release()

    asyncio.run(scenario())


def test_foreign_members_and_ancestor_waits_are_rejected() -> None:
    async def scenario() -> None:
        model = GatedModel(("done",))
        harness = Harness((model,))
        async with harness.group as group:
            await group.root.open_run("root-run")
            receipt = await group.root.spawn(spawn_request(), authority())
            await model.entered.get()
            with pytest.raises(CollaborationConflict, match="unavailable"):
                await group.root.send_message(
                    MessageRequest(
                        request_id="foreign", member_id="different-scope-member", content=envelope("message")
                    ),
                    authority(),
                )
            with pytest.raises(CollaborationConflict, match="strict descendant"):
                await group.endpoint(receipt.member_id).wait(
                    (group.root.member_id,), after=EventCursor(scope_id=group.state.scope_id), timeout_seconds=0
                )
            with pytest.raises(CollaborationConflict, match="another scope"):
                await group.root.wait((receipt.member_id,), after=EventCursor(scope_id="foreign"), timeout_seconds=0)
            model.release.release()

    asyncio.run(scenario())


class GatedAdmissionHost(Harness):
    def __init__(self, model: GatedModel) -> None:
        super().__init__((model,))
        self.admitting = asyncio.Event()
        self.accept = asyncio.Event()

    async def admit(self, proposal: WorkProposal, endpoint: MemberEndpoint) -> WorkAdmission:
        self.admitting.set()
        await self.accept.wait()
        return await super().admit(proposal, endpoint)


def test_cancelling_spawn_acknowledgement_does_not_lose_the_started_child() -> None:
    async def scenario() -> None:
        model = GatedModel(("unused",))
        harness = GatedAdmissionHost(model)
        async with harness.group as group:
            await group.root.open_run("root-run")
            async with asyncio.TaskGroup() as commands:
                pending = commands.create_task(group.root.spawn(spawn_request(), authority()))
                await harness.admitting.wait()
                pending.cancel()
                harness.accept.set()
                with pytest.raises(asyncio.CancelledError):
                    await pending
            await model.entered.get()
            original = await group.root.spawn(spawn_request(), authority())
            assert original.accepted and len(harness.admissions) == 1
            assert original.work_id in group.handles
        assert all(task.done() for task in group.tasks.values())
        assert not group.budgets.reservations

    asyncio.run(scenario())


def test_recovery_recovers_committed_receipt_and_parent_result_after_projection_was_lost() -> None:
    async def scenario() -> None:
        model = GatedModel(("durable answer",))
        live = Harness((model,))
        replica = InMemoryCollaborationStore()
        async with live.group as group:
            await group.root.open_run("root-run")
            accepted = await group.root.spawn(spawn_request(), authority())
            await model.entered.get()
            async with group.lock:
                await replica.write(group.state.model_copy(update={"revision": 1}))
            model.release.release()
            assert accepted.work_id is not None
            await terminal(group.root, accepted.member_id, accepted.work_id)
        recovered = Harness((), store=replica, sessions=live.sessions)
        await recovered.group.recover(expected_epoch=1)
        async with recovered.group as group:
            result = await group.root.read_result(accepted.work_id)
            assert result is not None and result.final_output == "durable answer"
            assert len(model.requests) == 1 and not recovered.admissions
            parent = await group.root_session.pending_inputs()
            results = tuple(value for value in parent if value.envelope.kind == "delegate_result")
            assert len(results) == 1
            charge = group.budgets.usage_charges("root-run")[0]
            assert charge.model_steps == charge.output_tokens == 1
            projected = tuple(event for event in group.state.events if event.kind == "run.step.completed")
            assert len(projected) == 1 and projected[0].source_sequence is not None

    asyncio.run(scenario())


def test_recovery_never_reexecutes_a_prepared_command_whose_host_acknowledgement_was_lost() -> None:
    async def scenario() -> None:
        harness = GatedAdmissionHost(GatedModel(("unused",)))
        replica = InMemoryCollaborationStore()
        async with harness.group as group:
            await group.root.open_run("root-run")
            async with asyncio.TaskGroup() as commands:
                pending = commands.create_task(group.root.spawn(spawn_request(), authority()))
                await harness.admitting.wait()
                saved = await group.store.read(group.state.scope_id)
                assert saved is not None
                await replica.write(saved.model_copy(update={"revision": 1}))
                harness.accept.set()
                await pending
        recovered = Harness((), store=replica)
        await recovered.group.recover(expected_epoch=1)
        async with recovered.group as group:
            await group.root.open_run("root-run")
            receipt = await group.root.spawn(spawn_request(), authority())
            assert not receipt.accepted and receipt.code == "recovery_interrupted"
            assert not recovered.admissions

    asyncio.run(scenario())


def test_followup_reauthorizes_image_history_before_another_provider_request() -> None:
    async def scenario() -> None:
        allowed = True

        async def resolver(reference: ImageReference, scope: ImageReadScope) -> ResolvedImage:
            if not allowed:
                raise PermissionError(f"Image access revoked for {scope.run_id}")
            return ResolvedImage(reference, IMAGE_BYTES)

        model = FakeOpenAIModel((text_batch("first image answer", "first"), text_batch("must not run", "second")))
        harness = Harness((model,), image_resolver=resolver)
        async with harness.group as group:
            await group.root.open_run("root-run")
            request = spawn_request().model_copy(
                update={
                    "content": InputEnvelope(
                        input_id="image",
                        kind="user",
                        source="host",
                        content=(ImageBlock(image=image_reference()),),
                    )
                }
            )
            first = await group.root.spawn(request, authority())
            assert first.work_id is not None
            await terminal(group.root, first.member_id, first.work_id)
            allowed = False
            second = await group.root.followup(
                MessageRequest(request_id="follow", member_id=first.member_id, content=envelope("inspect again")),
                authority(),
            )
            assert second.work_id is not None
            denied = await terminal(group.root, second.member_id, second.work_id)
            assert denied.receipt is not None and denied.receipt.state == "failed"
            assert len(model.requests) == 1

    asyncio.run(scenario())


class RootPauseModel(FakeOpenAIModel):
    def __init__(self) -> None:
        super().__init__(
            (
                tool_batch("spawn", name="spawn_agent", arguments=spawn_request().model_dump_json()),
                tool_batch("list", name="list_agents", arguments="{}"),
                text_batch("root resumed", "final"),
            )
        )
        self.entered: asyncio.Queue[None] = asyncio.Queue()
        self.release = asyncio.Semaphore(0)

    async def stream(self, request: OpenAIRequest | Mapping[str, object]) -> AsyncIterator[OpenAIStreamChunk]:
        if self.requests:
            await self.entered.put(None)
            await self.release.acquire()
        async for value in super().stream(request):
            yield value


@pytest.mark.parametrize("persistent", [False, True])
def test_cancelling_real_parent_run_commits_parent_and_child_receipts(tmp_path: Path, persistent: bool) -> None:
    async def scenario() -> None:
        child_model = GatedModel(("must not finish",))
        harness = Harness(
            (child_model,),
            store=SqliteCollaborationStore(tmp_path / "collaboration.sqlite3") if persistent else None,
            sessions=SqliteSessionStore(tmp_path / "sessions.sqlite3") if persistent else None,
            close_on_exit=False,
        )
        async with harness.group as group:
            root_model = RootPauseModel()
            registry = ToolRegistry()
            tools = collaboration_tools(group.root)
            for tool in tools:
                registry.register(tool.descriptor, partial(tool_factory, tool))
            runner = Runner(tools=registry, skills=SkillLoader(), tool_runtime=ToolRuntime())
            agent = Agent(
                name="root",
                model=root_model,
                prompt="Delegate work.",
                tools=tuple(ToolRef(tool.descriptor.code) for tool in tools),
            )
            await group.root_session.enqueue(envelope("start"))
            handle = await runner.start(
                agent,
                session=group.root_session,
                invocation_id="root-cancel",
                input_ids=("host-input",),
                input_mode="live",
                limits=RunLimits(),
            )
            async with asyncio.timeout(5):
                await child_model.entered.get()
                await root_model.entered.get()
                result = await handle.cancel(reason="host_shutdown")
            assert result.state == "cancelled" and result.reason == "host_shutdown"
            parent = await group.root_session.run_for_invocation("root-cancel")
            assert parent is not None and parent.receipt == result.receipt
            works = tuple(group.state.works.values())
            assert len(works) == 1 and works[0].receipt is not None
            child = works[0]
            assert child.receipt is not None and child.receipt.state == "cancelled"
            assert child.phase == "terminal" and child.resolution == "clear"
            assert not group.budgets.reservations
            assert group.budgets.usage_charges(handle.run_id)
            assert await group.root.read_result(child.proposal.work_id) == child.receipt

    asyncio.run(scenario())


def test_paused_root_restores_its_ledger_before_dispatching_original_child_resumption() -> None:
    async def scenario() -> None:
        tool = FunctionTool(
            ToolDescriptor(name="echo", code="echo", description="Request approval"), interrupting_tool, EchoArguments
        )
        child_model = FakeOpenAIModel((tool_batch("call-1"), text_batch("approved child", "child-final")))
        harness = Harness((child_model,), extra_tools=(tool,))
        async with harness.group as group:
            model = RootPauseModel()
            registry = ToolRegistry()
            tools = collaboration_tools(group.root)
            for bound in tools:
                registry.register(bound.descriptor, partial(tool_factory, bound))
            runner = Runner(tools=registry, skills=SkillLoader(), tool_runtime=ToolRuntime())
            agent = Agent(
                name="root",
                model=model,
                prompt="Coordinate approval.",
                tools=tuple(ToolRef(value.descriptor.code) for value in tools),
            )
            await group.root_session.enqueue(envelope("start"))
            limits = RunLimits()
            handle = await runner.start(
                agent,
                session=group.root_session,
                invocation_id="root-pause",
                input_ids=("host-input",),
                input_mode="live",
                limits=limits,
            )
            await model.entered.get()
            member = next(value for value in await group.root.list_members() if value.member_id != group.root.member_id)
            cursor = EventCursor(scope_id=group.state.scope_id)
            async with asyncio.timeout(5):
                while (await group.root.status(member.member_id))[0].phase != "paused":
                    cursor = (await group.root.wait((member.member_id,), after=cursor, timeout_seconds=1)).cursor
            requesting = asyncio.Event()

            async def pause_root() -> RunResult | RunSuspension:
                requesting.set()
                return await handle.pause(reason="approval_pending")

            async with asyncio.TaskGroup() as controls:
                pausing = controls.create_task(pause_root())
                await requesting.wait()
                model.release.release()
                paused = await pausing
            assert isinstance(paused, RunSuspension)
            work = (await group.root.status(member.member_id))[0]
            assert work.run_id is not None
            checkpoint = await group.session(member.member_id).checkpoint(work.run_id)
            assert checkpoint is not None
            now = datetime.now(UTC)
            decision = ResumeDecision(
                interruption_id=checkpoint.interruptions[0].interruption_id,
                result=ToolResult(
                    call_id="call-1", ok=True, output={"approved": True}, started_at=now, finished_at=now
                ),
            )
            context = ToolContext(
                call_id="host-resume",
                run_id=handle.run_id,
                grants=paused.checkpoint.grants,
                parent_limits=ParentRunLimits(
                    deadline=paused.checkpoint.deadline,
                    remaining_model_steps=16,
                    remaining_output_tokens=1024,
                    tool_concurrency=4,
                ),
            )
            await group.root.resume_work(work.proposal.work_id, context, decisions=(decision,))
            assert len(child_model.requests) == 1
            resumed = await runner.resume(
                agent, session=group.root_session, checkpoint=paused.checkpoint, limits=limits, decisions=()
            )
            await model.entered.get()
            child = await terminal(group.root, member.member_id, work.proposal.work_id)
            assert child.run_id == work.run_id and child.receipt is not None
            assert child.receipt.final_output == "approved child"
            model.release.release()
            root_result = await resumed.result()
            assert root_result.run_id == handle.run_id and root_result.state.value == "succeeded"
            assert len(group.budgets.usage_charges(handle.run_id)) == 1

    asyncio.run(scenario())


def test_member_and_depth_limits_reject_new_identities_before_host_admission() -> None:
    async def scenario() -> None:
        model = GatedModel(("done",))
        harness = Harness((model,), limits=CollaborationLimits(max_members=2, max_depth=1))
        async with harness.group as group:
            await group.root.open_run("root-run")
            parent = await group.root.spawn(spawn_request(), authority())
            await model.entered.get()
            work = (await group.root.status(parent.member_id))[0]
            assert work.run_id is not None
            rejected = await group.endpoint(parent.member_id).spawn(
                spawn_request("too-deep"), authority().model_copy(update={"run_id": work.run_id})
            )
            assert not rejected.accepted and rejected.code == "depth_limit"
            assert len(await group.root.list_members()) == 2
            model.release.release()
        limited = Harness((GatedModel(("done",)),), limits=CollaborationLimits(max_members=1))
        async with limited.group as group:
            await group.root.open_run("root-run")
            await group.root.spawn(spawn_request("first"), authority())
            rejected = await group.root.spawn(spawn_request("too-many"), authority())
            assert not rejected.accepted and rejected.code == "member_limit"
            assert len(limited.admissions) == 1

    asyncio.run(scenario())


def test_reserved_budget_prevents_a_second_child_from_starting() -> None:
    async def scenario() -> None:
        first, second = GatedModel(("done",)), FakeOpenAIModel((text_batch("must not run", "b"),))
        harness = Harness((first, second))
        context = authority()
        assert context.parent_limits is not None
        context = context.model_copy(
            update={
                "parent_limits": context.parent_limits.model_copy(
                    update={
                        "remaining_model_steps": 4,
                        "remaining_output_tokens": 128,
                    }
                )
            }
        )
        async with harness.group as group:
            await group.root.open_run("root-run")
            original = await group.root.spawn(spawn_request("first"), context)
            await first.entered.get()
            rejected = await group.root.spawn(spawn_request("second"), context)
            assert not rejected.accepted and rejected.code == "budget_exhausted"
            assert not second.requests
            first.release.release()
            assert original.work_id is not None
            await terminal(group.root, original.member_id, original.work_id)
            assert rejected == await group.root.spawn(spawn_request("second"), context)

    asyncio.run(scenario())


def test_closing_a_closed_member_with_a_new_request_does_not_reopen_its_lifecycle() -> None:
    async def scenario() -> None:
        harness = Harness((FakeOpenAIModel((text_batch("done", "a"),)),))
        async with harness.group as group:
            await group.root.open_run("root-run")
            accepted = await group.root.spawn(spawn_request(), authority())
            assert accepted.work_id is not None
            await terminal(group.root, accepted.member_id, accepted.work_id)
            await group.root.close_member(CloseRequest(request_id="close", member_id=accepted.member_id), authority())
            events = group.state.events
            repeated = await group.root.close_member(
                CloseRequest(request_id="close-again", member_id=accepted.member_id), authority()
            )
            assert repeated.accepted
            assert group.state.events == events
            assert group.state.members[accepted.member_id].state == "closed"

    asyncio.run(scenario())


def test_closed_scope_rejects_a_new_runner_lifecycle() -> None:
    async def scenario() -> None:
        harness = Harness(())
        async with harness.group:
            endpoint = harness.group.root
        with pytest.raises(CollaborationConflict, match="no active owner"):
            await endpoint.open_run("late-root-run")

    asyncio.run(scenario())


def test_collaboration_dispatches_unbounded_work_without_a_deadline_and_keeps_response_ceiling() -> None:
    class UnlimitedHarness(Harness):
        async def admit(self, proposal: WorkProposal, endpoint: MemberEndpoint) -> WorkAdmission:
            admitted = await super().admit(proposal, endpoint)
            admitted.binding.runner.host_policy = HostPolicy(
                timeout_seconds=None,
                max_model_steps=None,
                max_output_tokens=None,
            )
            return WorkAdmission(
                admitted.permit.model_copy(
                    update={
                        "limits": RunLimits(
                            execution_timeout_seconds=None,
                            max_model_steps=None,
                            max_output_tokens=None,
                            max_response_output_tokens=55,
                        )
                    }
                ),
                admitted.binding,
            )

    async def scenario() -> None:
        model = GatedModel(("done",))
        harness = UnlimitedHarness((model,))
        context = authority().model_copy(
            update={
                "parent_limits": ParentRunLimits(
                    deadline=None,
                    remaining_model_steps=None,
                    remaining_output_tokens=None,
                    max_response_output_tokens=33,
                    tool_concurrency=2,
                )
            }
        )
        async with harness.group as group:
            await group.root.open_run("root-run")
            accepted = await group.root.spawn(spawn_request(), context)
            await asyncio.wait_for(model.entered.get(), 3)
            assert accepted.work_id is not None
            work = group.state.works[accepted.work_id]
            assert work.permit is not None
            assert work.permit.limits.deadline is None
            assert work.permit.limits.max_model_steps is None
            assert work.permit.limits.max_output_tokens is None
            assert work.permit.limits.max_response_output_tokens == 33
            model.release.release()
            finished = await terminal(group.root, accepted.member_id, accepted.work_id)
            assert finished.receipt is not None and finished.receipt.final_output == "done"
            assert model.requests[0].max_completion_tokens == 33
            assert group.budgets.usage_charges("root-run")[0].model_steps == 1

    asyncio.run(scenario())
