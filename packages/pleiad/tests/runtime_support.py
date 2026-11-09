import json
from uuid import uuid4

from pleiad.agent.agent import Agent
from pleiad.context.selection.models import ContextBlock
from pleiad.messages.message import UserMessage
from pleiad.runtime.checkpoint import ResumeDecision
from pleiad.runtime.execution.control import RuntimeHandoffBinding
from pleiad.runtime.handle import RunHandle
from pleiad.runtime.limits import RunLimits
from pleiad.runtime.policy import EffectivePolicy
from pleiad.runtime.result import RunOutcome
from pleiad.runtime.runner import Runner
from pleiad.sessions.backends.in_memory import InMemorySessionStore
from pleiad.sessions.checkpoint import RunCheckpoint
from pleiad.sessions.input import InputEnvelope
from pleiad.sessions.model_step import ModelStep
from pleiad.sessions.session import Session


async def start_agent(
    runner: Runner,
    agent: Agent,
    user_input: str | UserMessage,
    *,
    session: Session | None = None,
    limits: RunLimits | None = None,
    context: tuple[ContextBlock, ...] = (),
    handoffs: tuple[RuntimeHandoffBinding, ...] = (),
    parent_run_id: str | None = None,
    parent_policy: EffectivePolicy | None = None,
) -> RunHandle:
    """Accept a real input and start its sole owned RunHandle."""
    invocation_id = uuid4().hex
    selected = Session(f"test:{invocation_id}", InMemorySessionStore()) if session is None else session
    input_id = user_input.message_id if isinstance(user_input, UserMessage) else f"{invocation_id}:input"
    content = user_input.content if isinstance(user_input, UserMessage) else user_input
    if not isinstance(content, str):
        raise ValueError("The text Session input contract requires string content")
    await selected.enqueue(InputEnvelope(input_id=input_id, kind="user", source="test", content=content))
    return await runner.start(
        agent,
        session=selected,
        invocation_id=invocation_id,
        input_ids=(input_id,),
        input_mode="batch",
        limits=RunLimits() if limits is None else limits,
        context=context,
        handoffs=handoffs,
        parent_run_id=parent_run_id,
        parent_policy=parent_policy,
    )


async def run_agent(
    runner: Runner,
    agent: Agent,
    user_input: str | UserMessage,
    *,
    session: Session | None = None,
    limits: RunLimits | None = None,
    context: tuple[ContextBlock, ...] = (),
    handoffs: tuple[RuntimeHandoffBinding, ...] = (),
    parent_run_id: str | None = None,
    parent_policy: EffectivePolicy | None = None,
) -> RunOutcome:
    """Await one real input through the public Session and Handle boundary."""
    handle = await start_agent(
        runner,
        agent,
        user_input,
        session=session,
        limits=limits,
        context=context,
        handoffs=handoffs,
        parent_run_id=parent_run_id,
        parent_policy=parent_policy,
    )
    async with handle:
        return await handle.result()


async def resume_agent(
    runner: Runner,
    agent: Agent,
    checkpoint: RunCheckpoint,
    *,
    session: Session,
    decisions: tuple[ResumeDecision, ...] = (),
    limits: RunLimits | None = None,
    handoffs: tuple[RuntimeHandoffBinding, ...] = (),
    parent_policy: EffectivePolicy | None = None,
) -> RunOutcome:
    """Resume the original Run through its public checkpoint and Handle."""
    handle = await runner.resume(
        agent,
        session=session,
        checkpoint=checkpoint,
        decisions=decisions,
        limits=RunLimits() if limits is None else limits,
        handoffs=handoffs,
        parent_policy=parent_policy,
    )
    async with handle:
        return await handle.result()


def input_text(value: object) -> str:
    """Read source text from the SDK's model-visible input envelope."""
    if not isinstance(value, str):
        raise TypeError("An input envelope must be serialized text")
    parsed: object = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("An input envelope must be an object")
    content: object = parsed.get("content")
    if not isinstance(content, str):
        raise ValueError("An input envelope must contain text content")
    return content


async def active_step(session: Session) -> ModelStep | None:
    """Read the persisted active step without acquiring its writer ownership."""
    snapshot = await session.store.snapshot(session.session_id)
    return next((value for value in snapshot.steps if value.state == "active"), None)
