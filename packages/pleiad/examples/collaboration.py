import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from functools import partial

from pleiad.agent.agent import Agent
from pleiad.budgets import BudgetLedger
from pleiad.contracts import ContractModel, JsonObject, ToolRef
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
from pleiad.orchestration.collaboration.backends.in_memory import InMemoryCollaborationStore
from pleiad.orchestration.collaboration.binding import CollaborationHost, WorkAdmission, WorkBinding
from pleiad.orchestration.collaboration.contracts import (
    CloseRequest,
    EventCursor,
    InterruptRequest,
    MessageRequest,
    SpawnRequest,
    WorkPermit,
    WorkProposal,
    WorkRejection,
)
from pleiad.orchestration.collaboration.endpoint import MemberEndpoint
from pleiad.orchestration.collaboration.group import CollaborationGroup
from pleiad.orchestration.collaboration.state import WorkRecord
from pleiad.orchestration.collaboration.tools import collaboration_tools
from pleiad.runtime.limits import RunLimits
from pleiad.runtime.runner import Runner
from pleiad.sessions.backends.in_memory import InMemorySessionStore
from pleiad.sessions.input import InputEnvelope
from pleiad.sessions.session import Session
from pleiad.skills.loader import SkillLoader
from pleiad.tools.base import FunctionTool
from pleiad.tools.contracts import ToolContext, ToolDescriptor
from pleiad.tools.registry import ToolRegistry
from pleiad.tools.runtime import ToolRuntime


def input_text(text: str) -> InputEnvelope:
    """Construct a host input; collaboration assigns its durable delivery identity."""
    return InputEnvelope(input_id="request-input", kind="user", source="demo", content=text)


class DemoModel(OpenAIModelClient):
    """Provide deterministic offline model responses through the real model protocol."""

    def __init__(self, *, coordinator: bool = False) -> None:
        """Choose the coordinator script or a reusable worker transport."""
        self.provider = "offline-demo"
        self.model = "collaboration-demo"
        self.image_input = "unverified"
        self.binding_fingerprint = binding_fingerprint(
            protocol="openai", provider=self.provider, model=self.model, base_url=None, provider_options={}
        )
        self.coordinator = coordinator
        self.calls = 0

    async def stream(self, request: OpenAIRequest | Mapping[str, object]) -> AsyncIterator[OpenAIStreamChunk]:
        """Emit scripted external responses without contacting a provider."""
        if not isinstance(request, OpenAIRequest):
            raise TypeError("The demonstration expects a validated OpenAI request")
        self.calls += 1
        if not self.coordinator and self.calls == 3:
            await asyncio.Event().wait()
        if self.coordinator and self.calls <= 2:
            name = "spawn_agent" if self.calls == 1 else "demonstrate_followup"
            arguments = (
                SpawnRequest(
                    request_id="spawn-worker",
                    profile_id="researcher",
                    title="Researcher",
                    content=input_text("Inspect the design"),
                ).model_dump(mode="json")
                if self.calls == 1
                else {}
            )
            delta = OpenAIStreamDelta(
                tool_calls=(
                    OpenAIStreamToolCall(
                        index=0,
                        id=f"call-{self.calls}",
                        function=OpenAIStreamFunction(name=name, arguments=json.dumps(arguments)),
                    ),
                )
            )
            finish = "tool_calls"
        else:
            delta = OpenAIStreamDelta(content=f"Completed model step {self.calls}.")
            finish = "stop"
        yield OpenAIStreamChunk(model=self.model, choices=(OpenAIStreamChoice(delta=delta),))
        yield OpenAIStreamChunk(
            model=self.model,
            choices=(OpenAIStreamChoice(delta=OpenAIStreamDelta(), finish_reason=finish),),
            usage=OpenAIUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        )


def load_tool(tool: FunctionTool, descriptor: ToolDescriptor) -> FunctionTool:
    """Retain one real endpoint scope across all registry loads."""
    if tool.descriptor != descriptor:
        raise ValueError("Unexpected Tool descriptor")
    return tool


def binding(endpoint: MemberEndpoint, model: DemoModel, extra: tuple[FunctionTool, ...] = ()) -> WorkBinding:
    """Bind actual Agent tools and Runner resources for one trusted member."""
    registry = ToolRegistry()
    tools = (*collaboration_tools(endpoint), *extra)
    for tool in tools:
        registry.register(tool.descriptor, partial(load_tool, tool))
    runner = Runner(tools=registry, skills=SkillLoader(), tool_runtime=ToolRuntime())
    agent = Agent(
        name="demo-agent",
        model=model,
        prompt="Coordinate the task.",
        tools=tuple(ToolRef(value.descriptor.code) for value in tools),
    )
    return WorkBinding(agent, runner, "demo-v1")


class DemoHost:
    """Keep profile policy and concrete execution resources outside the SDK."""

    def __init__(self) -> None:
        """Create a local resource owner for the demonstration."""
        self.bindings: dict[str, WorkBinding] = {}

    async def admit(self, proposal: WorkProposal, endpoint: MemberEndpoint) -> WorkAdmission | WorkRejection:
        """Authorize the profile and register a stable work identity before execution."""
        if proposal.profile_id != "researcher":
            return WorkRejection(code="profile_denied", message="Only the researcher profile is authorized.")
        if endpoint.member_id not in self.bindings:
            self.bindings[endpoint.member_id] = binding(endpoint, DemoModel())
        selected = self.bindings[endpoint.member_id]
        return WorkAdmission(
            WorkPermit(
                authorization_ref=proposal.work_id,
                binding_key=selected.binding_key,
                limits=RunLimits(max_model_steps=4, max_output_tokens=128),
            ),
            selected,
        )

    async def rebind(self, proposal: WorkProposal, permit: WorkPermit, endpoint: MemberEndpoint) -> WorkBinding:
        """Check the current authorization before dequeue or checkpoint resumption."""
        if permit.authorization_ref != proposal.work_id:
            raise ValueError("Authorization no longer matches the original work")
        return self.bindings[endpoint.member_id]


async def wait_phase(endpoint: MemberEndpoint, member_id: str, work_id: str, phase: str) -> WorkRecord:
    """Observe progress through a durable cursor without consuming the result."""
    cursor = EventCursor(scope_id=endpoint.group.state.scope_id)
    async with asyncio.timeout(10):
        while True:
            work = next(value for value in await endpoint.status(member_id) if value.proposal.work_id == work_id)
            if work.phase == phase:
                return work
            cursor = (await endpoint.wait((member_id,), after=cursor, timeout_seconds=1)).cursor


class DemoArguments(ContractModel):
    """Run the remaining host-controlled API examples inside a real root Run."""


async def main() -> None:
    """Run spawn, message, followup, interrupt, wait, and close without network access."""
    sessions = InMemorySessionStore()
    root_session = Session("demo-root", sessions)
    host = DemoHost()
    async with CollaborationGroup(
        "demo",
        root_session=root_session,
        sessions=sessions,
        store=InMemoryCollaborationStore(),
        host=CollaborationHost(host.admit, host.rebind),
        budgets=BudgetLedger(),
    ) as group:

        async def demonstrate(context: ToolContext, arguments: JsonObject) -> JsonObject:
            DemoArguments.model_validate(arguments)
            members = await group.root.list_members()
            child = next(member for member in members if member.parent_member_id == group.root.member_id)
            initial = (await group.root.status(child.member_id))[0]
            await wait_phase(group.root, child.member_id, initial.proposal.work_id, "terminal")
            await group.root.send_message(
                MessageRequest(
                    request_id="note", member_id=child.member_id, content=input_text("Keep the previous result")
                ),
                context,
            )
            follow = await group.root.followup(
                MessageRequest(
                    request_id="follow", member_id=child.member_id, content=input_text("Continue the analysis")
                ),
                context,
            )
            if follow.work_id is None:
                raise ValueError("Followup was not admitted")
            await wait_phase(group.root, child.member_id, follow.work_id, "terminal")
            interruptible = await group.root.followup(
                MessageRequest(
                    request_id="long-work", member_id=child.member_id, content=input_text("Start another task")
                ),
                context,
            )
            if interruptible.work_id is None:
                raise ValueError("Interruptible work was not admitted")
            running = await wait_phase(group.root, child.member_id, interruptible.work_id, "running")
            if running.run_id is None:
                raise ValueError("The accepted work has no Run")
            await group.root.interrupt(
                InterruptRequest(
                    request_id="interrupt",
                    member_id=child.member_id,
                    expected_work_id=interruptible.work_id,
                    expected_run_id=running.run_id,
                ),
                context,
            )
            await group.root.close_member(CloseRequest(request_id="close", member_id=child.member_id), context)
            return {
                "member_id": child.member_id,
                "session_id": child.session_id,
                "works": [value.phase for value in await group.root.status(child.member_id)],
            }

        demo = FunctionTool(
            ToolDescriptor(
                name="demonstrate_followup",
                code="demonstrate_followup",
                description="Demonstrate the host collaboration API.",
            ),
            demonstrate,
            DemoArguments,
        )
        root = binding(group.root, DemoModel(coordinator=True), (demo,))
        await root_session.enqueue(input_text("Coordinate this task"))
        handle = await root.runner.start(
            root.agent,
            session=root_session,
            invocation_id="demo-root-run",
            input_ids=("request-input",),
            input_mode="live",
            limits=RunLimits(),
        )
        async with handle:
            outcome = await handle.result()
        print(f"Root: {outcome.state.value}")
        for work in group.state.works.values():
            print(f"Work {work.proposal.work_id}: {work.phase}, Run {work.run_id}")


if __name__ == "__main__":
    asyncio.run(main())
