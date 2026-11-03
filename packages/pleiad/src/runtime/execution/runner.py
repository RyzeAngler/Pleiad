import asyncio
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from datetime import UTC, datetime
from uuid import uuid4

from jsonschema import Draft202012Validator

from pleiad.agent.agent import Agent
from pleiad.agent.errors import AgentConfigurationError
from pleiad.context.budget import BudgetGuard
from pleiad.context.compaction.compactor import ContextCompactor, HistorySummarizer
from pleiad.context.compaction.models import CompactionState
from pleiad.context.selection.models import ContextBlock
from pleiad.contracts import CapabilityGrants, JsonObject, JsonValue, thaw_json_object
from pleiad.events.contracts import Event, EventSink
from pleiad.events.message import MessageCompletedEvent
from pleiad.events.runtime import (
    RunCancelledEvent,
    RunCompletedEvent,
    RunFailedEvent,
    RunInterruptedEvent,
    RunLimitReachedEvent,
    RunResumedEvent,
    RunStartedEvent,
    RunTimedOutEvent,
    RuntimeEvent,
    ToolCompletedEvent,
    ToolInterruptedEvent,
    TurnCompletedEvent,
    TurnStartedEvent,
)
from pleiad.memory.short_memory import ShortMemory
from pleiad.memory.turn import Turn
from pleiad.messages.message import AssistantMessage, UserMessage
from pleiad.messages.message import ToolCall as MessageToolCall
from pleiad.observability.attributes import AGENT_NAME, PARENT_RUN_ID, RUN_ID, RUN_NAME
from pleiad.observability.tracing import Observability
from pleiad.runtime.config import RunConfig
from pleiad.runtime.errors import RuntimeStateError
from pleiad.runtime.execution.control import RuntimeAgentBinding, RuntimeHandoffBinding
from pleiad.runtime.execution.loop import AgentLoop, LoopOutcome, abort_safely, add_tool_usage
from pleiad.runtime.execution.model_input import tool_result_message
from pleiad.runtime.execution.stream import RunStream
from pleiad.runtime.lifecycle.result import RunResult
from pleiad.runtime.lifecycle.state import RunState, RunStatus, RunUsage
from pleiad.runtime.policy import EffectivePolicy, HostPolicy, effective_policy
from pleiad.skills.contracts import Skill
from pleiad.skills.loader import SkillLoader
from pleiad.tools.base import BaseTool
from pleiad.tools.builtins.read_file import ReadFileTool
from pleiad.tools.contracts import ParentRunLimits, ToolContext, ToolResult
from pleiad.tools.interruption import InterruptionResponse, ToolInterruption
from pleiad.tools.registry import ToolRegistry
from pleiad.tools.runtime import ToolRuntime

logger = logging.getLogger(__name__)


class Runner:
    """Execute new and resumed Agent Runs against one ShortMemory."""

    def __init__(
        self,
        *,
        tools: ToolRegistry,
        skills: SkillLoader,
        tool_runtime: ToolRuntime,
        event_sink: EventSink | None = None,
        host_policy: HostPolicy | None = None,
        loop: AgentLoop | None = None,
        history_summarizer: HistorySummarizer | None = None,
        observability: Observability | None = None,
    ) -> None:
        """Bind concrete SDK dependencies and process-owner policy."""
        self.tools = tools
        self.skills = skills
        self.tool_runtime = tool_runtime
        self.event_sink = event_sink
        self.host_policy = HostPolicy() if host_policy is None else host_policy
        self.observability = Observability() if observability is None else observability
        self.loop = AgentLoop(tool_runtime, observability=self.observability) if loop is None else loop
        self.loop.observability = self.observability
        self.history_summarizer = history_summarizer

    async def run(
        self,
        agent: Agent,
        user_input: str | UserMessage,
        *,
        memory: ShortMemory,
        context_blocks: tuple[ContextBlock, ...] = (),
        config: RunConfig,
        handoffs: tuple[RuntimeHandoffBinding, ...] = (),
        parent_run_id: str | None = None,
        parent_policy: EffectivePolicy | None = None,
    ) -> RunResult:
        """Start a new Run after persisting the original User message."""
        user_message = normalize_user_input(user_input)
        return await self.execute_run(
            agent,
            memory=memory,
            config=config,
            context_blocks=context_blocks,
            user_message=user_message,
            checkpoint=None,
            resolutions=(),
            event_sink=combined_event_sink(self.event_sink, None),
            handoffs=handoffs,
            parent_run_id=parent_run_id,
            parent_policy=parent_policy,
        )

    async def resume(
        self,
        agent: Agent,
        checkpoint: RunState,
        *,
        memory: ShortMemory,
        resolutions: tuple[InterruptionResponse, ...] = (),
        context_blocks: tuple[ContextBlock, ...] = (),
        config: RunConfig,
        handoffs: tuple[RuntimeHandoffBinding, ...] = (),
        parent_run_id: str | None = None,
        parent_policy: EffectivePolicy | None = None,
    ) -> RunResult:
        """Resume the active Turn described by a validated Runtime checkpoint."""
        return await self.execute_run(
            agent,
            memory=memory,
            config=config,
            context_blocks=context_blocks,
            user_message=None,
            checkpoint=checkpoint,
            resolutions=resolutions,
            event_sink=combined_event_sink(self.event_sink, None),
            handoffs=handoffs,
            parent_run_id=parent_run_id,
            parent_policy=parent_policy,
        )

    def run_streamed(
        self,
        agent: Agent,
        user_input: str | UserMessage,
        *,
        memory: ShortMemory,
        context_blocks: tuple[ContextBlock, ...] = (),
        config: RunConfig,
        handoffs: tuple[RuntimeHandoffBinding, ...] = (),
        parent_run_id: str | None = None,
        parent_policy: EffectivePolicy | None = None,
    ) -> RunStream:
        """Start a new Run and expose the same execution through Event."""
        return self.create_stream(
            lambda stream_sink: self.execute_run(
                agent,
                memory=memory,
                context_blocks=context_blocks,
                config=config,
                user_message=normalize_user_input(user_input),
                checkpoint=None,
                resolutions=(),
                event_sink=combined_event_sink(self.event_sink, stream_sink),
                handoffs=handoffs,
                parent_run_id=parent_run_id,
                parent_policy=parent_policy,
            ),
        )

    def resume_streamed(
        self,
        agent: Agent,
        checkpoint: RunState,
        *,
        memory: ShortMemory,
        resolutions: tuple[InterruptionResponse, ...] = (),
        context_blocks: tuple[ContextBlock, ...] = (),
        config: RunConfig,
        handoffs: tuple[RuntimeHandoffBinding, ...] = (),
        parent_run_id: str | None = None,
        parent_policy: EffectivePolicy | None = None,
    ) -> RunStream:
        """Resume a Run and expose the same execution through Event."""
        return self.create_stream(
            lambda stream_sink: self.execute_run(
                agent,
                memory=memory,
                resolutions=resolutions,
                context_blocks=context_blocks,
                config=config,
                user_message=None,
                checkpoint=checkpoint,
                event_sink=combined_event_sink(self.event_sink, stream_sink),
                handoffs=handoffs,
                parent_run_id=parent_run_id,
                parent_policy=parent_policy,
            ),
        )

    def create_stream(self, execution: Callable[[EventSink], Awaitable[RunResult]]) -> RunStream:
        """Create a RunStream around one event-aware execution factory."""
        events_queue: asyncio.Queue[Event | None] = asyncio.Queue(maxsize=256)

        async def publish(event: Event) -> None:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                with suppress(asyncio.QueueFull):
                    events_queue.put_nowait(event)
                return
            await events_queue.put(event)

        async def consume() -> RunResult:
            return await execution(publish)

        task = asyncio.get_running_loop().create_task(consume())
        return RunStream(events_queue, task)

    async def execute_run(
        self,
        agent: Agent,
        *,
        memory: ShortMemory,
        config: RunConfig,
        context_blocks: tuple[ContextBlock, ...],
        user_message: UserMessage | None,
        checkpoint: RunState | None,
        resolutions: tuple[InterruptionResponse, ...],
        event_sink: EventSink | None,
        handoffs: tuple[RuntimeHandoffBinding, ...],
        parent_run_id: str | None,
        parent_policy: EffectivePolicy | None,
    ) -> RunResult:
        """Run the one shared path used by new, resumed, streamed, and non-streamed calls."""
        initial_binding = self.resolve_binding(agent)
        policy = effective_policy(
            config,
            self.host_policy,
            agent_hints=agent.policy_hints,
            parent=parent_policy,
        )
        runtime_handoffs = self.prepare_handoffs(handoffs, policy)
        bindings = {agent.name: initial_binding}
        bindings.update({binding.target.agent.name: binding.target for binding in runtime_handoffs})
        if checkpoint is None:
            if user_message is None:
                raise ValueError("a new Run requires user_input")
            active_turn = await memory.begin(agent.name, (user_message,))
            run_id = uuid4().hex
            usage = RunUsage()
            compaction_state = None
            active_binding = initial_binding
            handoff_count = 0
            is_new_run = True
        else:
            if parent_run_id is not None and parent_run_id != checkpoint.parent_run_id:
                raise RuntimeStateError("The checkpoint parent Run identity changed before resume.")
            if checkpoint.parent_run_id is not None and parent_policy is None:
                raise RuntimeStateError("Resuming a child Run requires the current parent policy.")
            parent_run_id = checkpoint.parent_run_id
            policy = self.resume_policy(checkpoint, policy)
            runtime_handoffs = self.prepare_handoffs(handoffs, policy)
            bindings = {agent.name: initial_binding}
            bindings.update({binding.target.agent.name: binding.target for binding in runtime_handoffs})
            checkpoint_binding = bindings.get(checkpoint.active_agent)
            if checkpoint_binding is None:
                raise RuntimeStateError("The checkpoint active Agent is not bound for this Run.")
            active_binding = checkpoint_binding
            await self.emit(RunResumedEvent(run_id=checkpoint.run_id, agent_name=active_binding.agent.name), event_sink)
            run_id, active_turn, usage, compaction_state, pending_result = await self.prepare_resume(
                agent,
                active_binding,
                checkpoint,
                resolutions,
                memory,
                active_binding.tools,
                active_binding.fingerprint,
                policy,
                event_sink,
            )
            if pending_result is not None:
                with self.observability.span(
                    RUN_NAME,
                    attributes={
                        RUN_ID: checkpoint.run_id,
                        AGENT_NAME: active_binding.agent.name,
                        **({PARENT_RUN_ID: parent_run_id} if parent_run_id is not None else {}),
                    },
                    observation_type="agent",
                ) as run_span:
                    self.observability.finish(run_span, pending_result.status.value, error=pending_result.error)
                    self.observability.metrics.record_run(pending_result.status.value)
                    logger.info(f"run ended run={checkpoint.run_id} status={pending_result.status.value}")
                    await self.emit_terminal(pending_result, event_sink)
                    return pending_result
            handoff_count = checkpoint.handoff_count
            is_new_run = False
        if is_new_run:
            try:
                await self.emit(RunStartedEvent(run_id=run_id, agent_name=agent.name), event_sink)
                await self.emit(
                    TurnStartedEvent(run_id=run_id, turn_id=active_turn.turn_id, agent_name=agent.name), event_sink
                )
                if user_message is not None:
                    await self.emit(
                        MessageCompletedEvent(
                            run_id=run_id,
                            turn_id=active_turn.turn_id,
                            agent_name=agent.name,
                            message=user_message,
                        ),
                        event_sink,
                    )
            except asyncio.CancelledError:
                await abort_safely(memory, active_turn.turn_id, "startup_cancelled")
                await self.emit(RunCancelledEvent(run_id=run_id), event_sink)
                raise
        logger.info(f"run started run={run_id} agent={agent.name}")
        with self.observability.span(
            RUN_NAME,
            attributes={
                RUN_ID: run_id,
                AGENT_NAME: agent.name,
                **({PARENT_RUN_ID: parent_run_id} if parent_run_id is not None else {}),
            },
            observation_type="agent",
        ) as run_span:
            try:
                outcome = await self.loop.run(
                    active_binding.agent,
                    run_id=run_id,
                    memory=memory,
                    active_turn_id=active_turn.turn_id,
                    user_message=user_message,
                    model=active_binding.agent.model,
                    tools=active_binding.tools,
                    skills=active_binding.skills,
                    policy=policy,
                    compactor=ContextCompactor(BudgetGuard(config.context_budget), self.loop.context_renderer),
                    context_blocks=context_blocks,
                    agent_fingerprint=active_binding.fingerprint,
                    usage=usage,
                    compaction_state=compaction_state,
                    event_sink=event_sink,
                    history_summarizer=self.history_summarizer,
                    root_agent_name=agent.name,
                    handoffs=runtime_handoffs,
                    handoff_count=handoff_count,
                    parent_run_id=parent_run_id,
                    max_handoffs=(
                        config.max_handoffs if checkpoint is None else min(config.max_handoffs, checkpoint.max_handoffs)
                    ),
                )
            except asyncio.CancelledError:
                self.observability.finish(run_span, "cancelled")
                self.observability.metrics.record_run("cancelled")
                logger.info(f"run ended run={run_id} status=cancelled")
                await self.emit(RunCancelledEvent(run_id=run_id), event_sink)
                raise
            except Exception:
                await self.emit(
                    RunFailedEvent(
                        run_id=run_id,
                        error_code="runtime.system_failure",
                        error_message="The Run stopped because of an internal failure.",
                    ),
                    event_sink,
                )
                raise
            run_result = project_outcome(outcome, run_id)
            self.observability.finish(run_span, run_result.status.value, error=run_result.error)
            self.observability.metrics.record_run(run_result.status.value)
            logger.info(f"run ended run={run_id} status={run_result.status.value}")
            await self.emit_terminal(run_result, event_sink)
            return run_result

    def resolve(self, agent: Agent) -> tuple[tuple[BaseTool, ...], tuple[Skill, ...]]:
        """Resolve capability bindings before a Run creates a new Turn."""
        try:
            tools = tuple(self.tools.load(ref.code) for ref in agent.tools)
            skills = tuple(self.skills.resolve(ref.code) for ref in agent.skills)
        except (LookupError, ValueError) as error:
            raise AgentConfigurationError(f"Agent capability binding is unavailable: {agent.name}.") from error
        if skills:
            read_tools = tuple(tool for tool in tools if tool.descriptor.code == "read_file")
            if not read_tools:
                raise AgentConfigurationError("Selected Skills require the read_file Tool.")
            read_tool = read_tools[0]
            if not isinstance(read_tool, ReadFileTool):
                raise AgentConfigurationError("Selected Skills require the SDK read_file Tool binding.")
            missing = tuple(
                skill.descriptor.code for skill in skills if skill.descriptor.code not in read_tool.skill_codes
            )
            if missing:
                missing_codes = ", ".join(missing)
                raise AgentConfigurationError(f"Selected Skills are not readable: {missing_codes}.")
        return tools, skills

    def resolve_binding(self, agent: Agent) -> RuntimeAgentBinding:
        """Resolve one Agent into the Runtime binding used for a Run."""
        tools, skills = self.resolve(agent)
        if any(
            tool.descriptor.name == "read_tool_result" or tool.descriptor.code == "read_tool_result" for tool in tools
        ):
            raise AgentConfigurationError("read_tool_result is reserved for the Runtime-bound result reader.")
        return RuntimeAgentBinding(
            agent=agent,
            tools=tools,
            skills=skills,
            fingerprint=agent_fingerprint(agent, tools, skills),
        )

    def bind_handoff(
        self,
        source: Agent,
        target: Agent,
        *,
        name: str,
        description: str,
    ) -> RuntimeHandoffBinding:
        """Compile a validated Orchestration Handoff into a Runtime binding."""
        self.resolve_binding(source)
        return RuntimeHandoffBinding(
            source_agent_name=source.name,
            target=self.resolve_binding(target),
            name=name,
            description=description,
        )

    def prepare_handoffs(
        self,
        handoffs: tuple[RuntimeHandoffBinding, ...],
        policy: EffectivePolicy,
    ) -> tuple[RuntimeHandoffBinding, ...]:
        """Attach the current Run policy to validated Handoff targets."""
        names: set[tuple[str, str]] = set()
        prepared: list[RuntimeHandoffBinding] = []
        for handoff in handoffs:
            if handoff.name == "read_tool_result":
                raise AgentConfigurationError("read_tool_result is reserved for the Runtime-bound result reader.")
            identity = (handoff.source_agent_name, handoff.name)
            if identity in names:
                raise AgentConfigurationError(f"Duplicate Handoff binding: {handoff.name}.")
            names.add(identity)
            target_policy = effective_policy(
                RunConfig(
                    timeout_seconds=policy.timeout_seconds,
                    max_turns=policy.max_turns,
                    max_output_tokens=policy.max_output_tokens,
                    tool_concurrency=policy.tool_concurrency,
                    grants=policy.grants,
                ),
                self.host_policy,
                agent_hints=handoff.target.agent.policy_hints,
                parent=policy,
            )
            prepared.append(handoff.with_policy(target_policy))
        return tuple(prepared)

    def resume_policy(self, checkpoint: RunState, current: EffectivePolicy) -> EffectivePolicy:
        """Restore the original Run budget instead of starting a new deadline."""
        deadline = min(checkpoint.deadline, current.deadline)
        now = datetime.now(UTC)
        return EffectivePolicy(
            timeout_seconds=max(0.0, (deadline - now).total_seconds()),
            max_turns=min(current.max_turns, checkpoint.max_turns),
            max_output_tokens=min(current.max_output_tokens, checkpoint.max_output_tokens),
            tool_concurrency=min(current.tool_concurrency, checkpoint.tool_concurrency),
            grants=current.grants.intersect(checkpoint.grants),
            deadline=deadline,
        )

    async def prepare_resume(
        self,
        root_agent: Agent,
        active_binding: RuntimeAgentBinding,
        checkpoint: RunState,
        resolutions: tuple[InterruptionResponse, ...],
        memory: ShortMemory,
        tools: tuple[BaseTool, ...],
        fingerprint: str,
        policy: EffectivePolicy,
        event_sink: EventSink | None,
    ) -> tuple[str, Turn, RunUsage, CompactionState | None, RunResult | None]:
        """Validate a checkpoint and complete only newly resolved Tool actions."""
        if checkpoint.version != 2:
            raise RuntimeStateError("The Runtime checkpoint schema version is unsupported.")
        if checkpoint.memory_id != memory.memory_id:
            raise RuntimeStateError("The Runtime checkpoint memory ID does not match ShortMemory.")
        if checkpoint.root_agent != root_agent.name or checkpoint.active_agent != active_binding.agent.name:
            raise RuntimeStateError("The Runtime checkpoint Agent identity does not match.")
        if checkpoint.agent_fingerprint != fingerprint:
            raise RuntimeStateError("The Agent fingerprint changed before the Run resumed.")
        active_turn = await memory.active()
        if active_turn is None or active_turn.turn_id != checkpoint.turn_id:
            raise RuntimeStateError("The active ShortMemory Turn does not match the checkpoint.")
        tools_by_code = {tool.descriptor.code: tool for tool in tools}
        assistant = next((message for message in active_turn.messages if isinstance(message, AssistantMessage)), None)
        if assistant is None:
            raise RuntimeStateError("The active Turn is missing its Assistant Tool declarations.")
        calls = assistant.tool_calls
        calls_by_id = {call.id: call for call in calls}
        known_call_ids = {item.call_id for item in checkpoint.interruptions} | {
            item.call_id for item in checkpoint.tool_results
        }
        if known_call_ids != set(calls_by_id):
            raise RuntimeStateError("Checkpoint Tool outcomes do not match the active Assistant declarations.")
        for interruption in checkpoint.interruptions:
            tool = tools_by_code.get(interruption.tool_code)
            call = calls_by_id.get(interruption.call_id)
            if tool is None or call is None or call.name != interruption.tool_name:
                raise RuntimeStateError("A pending Tool is no longer compatible with the checkpoint.")
            if parse_tool_arguments(call.arguments) != interruption.arguments:
                raise RuntimeStateError("A pending Tool argument contract changed before resume.")
            if Draft202012Validator(tool.input_schema).is_valid(interruption.arguments) is False:
                raise RuntimeStateError("A pending Tool schema changed before resume.")
            missing_permissions = tool.required_permissions - set(policy.grants.capabilities)
            if missing_permissions:
                raise RuntimeStateError("Current policy no longer permits a pending Tool.")
        validate_resolutions(checkpoint.interruptions, resolutions)
        checkpoint = checkpoint.model_copy(
            update={
                "deadline": policy.deadline,
                "max_turns": policy.max_turns,
                "max_output_tokens": policy.max_output_tokens,
                "tool_concurrency": policy.tool_concurrency,
                "grants": policy.grants,
            },
        )
        if policy.deadline <= datetime.now(UTC):
            result = RunResult(
                run_id=checkpoint.run_id,
                status=RunStatus.TIMED_OUT,
                usage=checkpoint.usage,
                checkpoint=checkpoint,
            )
            return checkpoint.run_id, active_turn, checkpoint.usage, checkpoint.compaction, result
        all_results = {result.call_id: result for result in checkpoint.tool_results}
        newly_resolved = 0
        unresolved: list[ToolInterruption] = []
        responses_by_id = {response.interruption_id: response for response in resolutions}
        for index, interruption in enumerate(checkpoint.interruptions):
            response = responses_by_id.get(interruption.interruption_id)
            if response is None:
                unresolved.append(interruption)
            else:
                tool = tools_by_code[interruption.tool_code]
                call = calls_by_id[interruption.call_id]
                outcome = await self.tool_runtime.resume_interruption(
                    tool,
                    interruption,
                    response,
                    ToolContext(
                        call_id=interruption.call_id,
                        run_id=checkpoint.run_id,
                        parent_run_id=checkpoint.parent_run_id,
                        turn_id=active_turn.turn_id,
                        grants=policy.grants,
                        parent_limits=ParentRunLimits(
                            deadline=policy.deadline,
                            remaining_turns=max(0, policy.max_turns - checkpoint.usage.model_calls),
                            remaining_output_tokens=max(
                                0, policy.max_output_tokens - checkpoint.usage.budget_output_tokens
                            ),
                            tool_concurrency=policy.tool_concurrency,
                        ),
                    ),
                    deadline=policy.deadline,
                )
                if outcome is None:
                    usage = add_tool_usage(checkpoint.usage, newly_resolved)
                    timed_out_checkpoint = checkpoint.model_copy(
                        update={
                            "deadline": min(policy.deadline, datetime.now(UTC)),
                            "interruptions": (*unresolved, *checkpoint.interruptions[index:]),
                            "tool_results": order_tool_results(calls, all_results),
                            "usage": usage,
                        },
                    )
                    result = RunResult(
                        run_id=checkpoint.run_id,
                        status=RunStatus.TIMED_OUT,
                        usage=usage,
                        checkpoint=timed_out_checkpoint,
                    )
                    return checkpoint.run_id, active_turn, usage, checkpoint.compaction, result
                if outcome.call_id != interruption.call_id:
                    raise RuntimeStateError("A resumed Tool returned a different call identity.")
                if isinstance(outcome, ToolInterruption):
                    if (
                        outcome.tool_code != interruption.tool_code
                        or outcome.tool_name != interruption.tool_name
                        or outcome.arguments != interruption.arguments
                    ):
                        raise RuntimeStateError("A resumed Tool changed its pending invocation.")
                    unresolved.append(outcome)
                    await self.emit(
                        ToolInterruptedEvent(
                            run_id=checkpoint.run_id,
                            turn_id=active_turn.turn_id,
                            assistant_message_id=assistant.message_id,
                            call_id=call.id,
                            tool_code=tool.descriptor.code,
                            interruption_id=outcome.interruption_id,
                        ),
                        event_sink,
                    )
                else:
                    all_results[response.result.call_id] = outcome
                    newly_resolved += 1
                    result_message = tool_result_message(
                        call,
                        outcome,
                        run_id=checkpoint.run_id,
                        turn_id=active_turn.turn_id,
                    )
                    await self.emit(
                        ToolCompletedEvent(
                            run_id=checkpoint.run_id,
                            turn_id=active_turn.turn_id,
                            assistant_message_id=assistant.message_id,
                            call_id=call.id,
                            tool_code=tool.descriptor.code,
                            result_message_id=result_message.message_id,
                            ok=outcome.ok,
                        ),
                        event_sink,
                    )
        usage = add_tool_usage(checkpoint.usage, newly_resolved)
        if unresolved:
            updated_checkpoint = checkpoint.model_copy(
                update={
                    "interruptions": tuple(unresolved),
                    "tool_results": order_tool_results(calls, all_results),
                    "usage": usage,
                },
            )
            result = RunResult(
                run_id=checkpoint.run_id,
                status=RunStatus.INTERRUPTED,
                usage=usage,
                checkpoint=updated_checkpoint,
                interruptions=tuple(unresolved),
            )
            return checkpoint.run_id, active_turn, usage, checkpoint.compaction, result
        tool_messages = tuple(
            tool_result_message(
                call,
                all_results[call.id],
                run_id=checkpoint.run_id,
                turn_id=active_turn.turn_id,
            )
            for call in calls
        )
        await memory.complete(active_turn.turn_id, tool_messages)
        for message in tool_messages:
            await self.emit(
                MessageCompletedEvent(
                    run_id=checkpoint.run_id,
                    turn_id=active_turn.turn_id,
                    agent_name=active_binding.agent.name,
                    message=message,
                ),
                event_sink,
            )
        await self.emit(
            TurnCompletedEvent(
                run_id=checkpoint.run_id,
                turn_id=active_turn.turn_id,
                agent_name=active_binding.agent.name,
            ),
            event_sink,
        )
        if usage.model_calls >= policy.max_turns or usage.budget_output_tokens >= policy.max_output_tokens:
            result = RunResult(run_id=checkpoint.run_id, status=RunStatus.LIMIT_REACHED, usage=usage)
            return checkpoint.run_id, active_turn, usage, checkpoint.compaction, result
        next_turn = await memory.begin(active_binding.agent.name)
        await self.emit(
            TurnStartedEvent(
                run_id=checkpoint.run_id,
                turn_id=next_turn.turn_id,
                agent_name=active_binding.agent.name,
            ),
            event_sink,
        )
        return checkpoint.run_id, next_turn, usage, checkpoint.compaction, None

    async def emit(self, event: RuntimeEvent, event_sink: EventSink | None) -> None:
        """Publish one Runtime event through the configured delivery path."""
        if event_sink is None:
            return
        await event_sink(event)

    async def emit_terminal(self, run_result: RunResult, event_sink: EventSink | None) -> None:
        """Publish the terminal event for a completed Runtime state transition."""
        if run_result.status is RunStatus.SUCCEEDED:
            event: RuntimeEvent = RunCompletedEvent(run_id=run_result.run_id)
        elif run_result.status is RunStatus.FAILED:
            if run_result.error is None:
                raise RuntimeError("A failed Run is missing its business error")
            event = RunFailedEvent(
                run_id=run_result.run_id,
                error_code=run_result.error.code,
                error_message=run_result.error.message,
            )
        elif run_result.status is RunStatus.INTERRUPTED:
            event = RunInterruptedEvent(
                run_id=run_result.run_id,
                interruption_ids=tuple(item.interruption_id for item in run_result.interruptions),
            )
        elif run_result.status is RunStatus.TIMED_OUT:
            event = RunTimedOutEvent(run_id=run_result.run_id)
        else:
            event = RunLimitReachedEvent(run_id=run_result.run_id)
        await self.emit(event, event_sink)


def combined_event_sink(first: EventSink | None, second: EventSink | None) -> EventSink | None:
    """Notify an optional observer before the reliable stream delivery sink."""
    if first is None and second is None:
        return None

    async def publish(event: Event) -> None:
        if first is not None:
            try:
                await first(event)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.error(f"run event sink failed kind={event.kind}", exc_info=True)
        if second is not None:
            await second(event)

    return publish


def normalize_user_input(user_input: str | UserMessage) -> UserMessage:
    """Normalize caller input while preserving an existing User Message exactly."""
    if isinstance(user_input, UserMessage):
        return user_input
    return UserMessage(content=user_input)


def agent_fingerprint(agent: Agent, tools: tuple[BaseTool, ...], skills: tuple[Skill, ...]) -> str:
    """Hash only non-secret configuration that affects recovery semantics."""
    model = agent.model
    payload = {
        "agent": {
            "name": agent.name,
            "prompt_digest": hashlib.sha256(agent.prompt.encode("utf-8")).hexdigest(),
            "model_type": type(model).__name__,
            "provider": model.provider,
            "model": model.model,
            "tools": [ref.code for ref in agent.tools],
            "skills": [ref.code for ref in agent.skills],
            "output_schema": thaw_json_object(agent.output_schema) if agent.output_schema is not None else None,
            "policy_hints": {
                "timeout_seconds": agent.policy_hints.timeout_seconds,
                "max_turns": agent.policy_hints.max_turns,
                "max_output_tokens": agent.policy_hints.max_output_tokens,
                "grants": grants_payload(agent.policy_hints.grants),
            },
        },
        "tools": [
            {
                "descriptor": tool.descriptor.model_dump(mode="json"),
                "input_schema": tool.input_schema,
                "policy": tool.policy.model_dump(mode="json"),
            }
            for tool in tools
        ],
        "skills": [
            {
                "descriptor": skill.descriptor.model_dump(mode="json"),
                "content_digest": skill.content_digest,
                "references": list(skill.references),
            }
            for skill in skills
        ],
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def grants_payload(grants: CapabilityGrants | None) -> JsonObject | None:
    """Serialize optional capability hints without exposing runtime objects."""
    if grants is None:
        return None
    return {
        "capabilities": grant_list(grants.capabilities),
        "resource_read": grant_list(grants.resource_read),
        "resource_write": grant_list(grants.resource_write),
    }


def grant_list(values: frozenset[str]) -> list[JsonValue]:
    """Project grant names into the recursive JSON contract."""
    names: list[JsonValue] = []
    for value in sorted(values):
        names.append(value)
    return names


def project_outcome(outcome: LoopOutcome, run_id: str) -> RunResult:
    """Construct the only public RunResult from the internal loop outcome."""
    return RunResult(
        run_id=run_id,
        status=outcome.status,
        final_output=outcome.final_output,
        structured_output=outcome.structured_output,
        usage=outcome.usage,
        checkpoint=outcome.checkpoint,
        interruptions=outcome.interruptions,
        error=outcome.error,
    )


def validate_resolutions(
    interruptions: tuple[ToolInterruption, ...],
    resolutions: tuple[InterruptionResponse, ...],
) -> None:
    """Reject duplicate, unknown, and mismatched interruption resolutions."""
    resolution_ids = tuple(item.interruption_id for item in resolutions)
    if len(resolution_ids) != len(set(resolution_ids)):
        raise ValueError("interruption resolution IDs must be unique")
    pending = {item.interruption_id: item for item in interruptions}
    for resolution in resolutions:
        interruption = pending.get(resolution.interruption_id)
        if interruption is None:
            raise ValueError("interruption resolution does not belong to this checkpoint")
        if resolution.result.call_id != interruption.call_id:
            raise ValueError("interruption resolution call ID does not match its pending Tool call")


def order_tool_results(
    calls: tuple[MessageToolCall, ...],
    results: Mapping[str, ToolResult],
) -> tuple[ToolResult, ...]:
    """Return pending Tool results in the model-declared call order."""
    ordered: list[ToolResult] = []
    for call in calls:
        if call.id in results:
            ordered.append(results[call.id])
    return tuple(ordered)


def parse_tool_arguments(arguments: str) -> JsonObject | None:
    """Parse a recovered Tool declaration at the execution boundary."""
    try:
        value: object = json.loads(arguments)
    except json.JSONDecodeError:
        return None
    if not isinstance(value, dict):
        return None
    return thaw_json_object(value)
