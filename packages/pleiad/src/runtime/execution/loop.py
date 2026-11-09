import asyncio
import hashlib
import json
import logging
import sys
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from uuid import uuid4

from jsonschema import Draft202012Validator
from opentelemetry.trace import Span

from pleiad.agent.agent import Agent
from pleiad.agent.errors import InvalidAgentOutputError
from pleiad.agent.prompt import build_prompt
from pleiad.context.budget import ContextBudget
from pleiad.context.compaction.compactor import ContextCompactor, HistorySummarizer, add_summary
from pleiad.context.compaction.models import CompactionState
from pleiad.context.rendering import ContextRenderer
from pleiad.context.selection.builder import ContextBuilder
from pleiad.context.selection.models import ContextBlock
from pleiad.contracts import CapabilityGrants, JsonObject, UsageCharge, thaw_json_object
from pleiad.errors.base import BusinessError
from pleiad.events.contracts import Event, EventSink
from pleiad.events.message import (
    MessageAbortedEvent,
    MessageCompletedEvent,
    MessageRefusalDeltaEvent,
    MessageStartedEvent,
    MessageTextDeltaEvent,
    MessageToolCallDeltaEvent,
)
from pleiad.events.runtime import (
    HandoffCompletedEvent,
    HandoffStartedEvent,
    ModelCompletedEvent,
    ModelFailedEvent,
    ModelStartedEvent,
    ModelStepCompletedEvent,
    ModelStepStartedEvent,
    ToolCompletedEvent,
    ToolInterruptedEvent,
    ToolStartedEvent,
)
from pleiad.logging.context import bind_log_context
from pleiad.messages.message import AssistantMessage, ToolMessage, UserMessage
from pleiad.messages.message import ToolCall as MessageToolCall
from pleiad.models.anthropic.client import AnthropicModelClient
from pleiad.models.contracts import Tool
from pleiad.models.openai.client import OpenAIModelClient
from pleiad.models.result import ModelResult, ModelStreamEventKind
from pleiad.observability.attributes import (
    AGENT_NAME,
    CALL_ID,
    CONTEXT_BUILD_NAME,
    CONTEXT_COMPACT_NAME,
    FIRST_OUTPUT_MS,
    MEMORY_READ_NAME,
    MODEL,
    MODEL_GENERATE_NAME,
    OPERATION,
    PARENT_RUN_ID,
    PROTOCOL,
    PROVIDER,
    RUN_ID,
    STEP_ID,
    STEP_NAME,
    TOOL_CODE,
    TOOL_EXECUTE_NAME,
    TOOL_NAME,
)
from pleiad.observability.tracing import Observability
from pleiad.runtime.errors import HandoffConflictError, HandoffLimitError
from pleiad.runtime.execution.control import RuntimeHandoffBinding
from pleiad.runtime.execution.control_state import RunControl
from pleiad.runtime.execution.model_input import ModelInput, build_model_input, stream_model, tool_result_message
from pleiad.runtime.execution.outcome import LoopOutcome, LoopStatus
from pleiad.runtime.execution.read_tool_result import ReadToolResultTool
from pleiad.runtime.operations import OperationScope
from pleiad.runtime.policy import EffectivePolicy
from pleiad.sessions.checkpoint import RunCheckpoint
from pleiad.sessions.records import ToolCallRecord
from pleiad.sessions.store import complete_owned
from pleiad.sessions.usage import RunUsage
from pleiad.sessions.writer import SessionWriter
from pleiad.skills.contracts import Skill
from pleiad.tools.base import BaseTool
from pleiad.tools.contracts import ParentRunLimits, ToolCall, ToolContext, ToolResult
from pleiad.tools.errors import InvalidToolArgumentsError, UnknownToolError
from pleiad.tools.interruption import ToolExecutionOutcome, ToolInterruption
from pleiad.tools.runtime import ToolRuntime

logger = logging.getLogger(__name__)


class AgentLoop:
    """Drive one SessionWriter-backed Model to Tool to Model execution path."""

    def __init__(
        self,
        tool_runtime: ToolRuntime,
        *,
        context_builder: ContextBuilder | None = None,
        context_renderer: ContextRenderer | None = None,
        observability: Observability | None = None,
    ) -> None:
        """Create an AgentLoop with SDK-owned context and prompt components."""
        self.tool_runtime = tool_runtime
        self.context_builder = ContextBuilder() if context_builder is None else context_builder
        self.context_renderer = ContextRenderer() if context_renderer is None else context_renderer
        self.observability = Observability() if observability is None else observability

    async def run(
        self,
        agent: Agent,
        *,
        run_id: str,
        memory: SessionWriter,
        active_step_id: str,
        control: RunControl,
        operations: OperationScope,
        model: OpenAIModelClient | AnthropicModelClient,
        tools: tuple[BaseTool, ...],
        skills: tuple[Skill, ...],
        policy: EffectivePolicy,
        compactor: ContextCompactor,
        context_blocks: tuple[ContextBlock, ...],
        agent_fingerprint: str,
        usage: RunUsage,
        compaction_state: CompactionState | None,
        event_sink: EventSink | None,
        history_summarizer: HistorySummarizer | None = None,
        root_agent_name: str | None = None,
        handoffs: tuple[RuntimeHandoffBinding, ...] = (),
        handoff_count: int = 0,
        parent_run_id: str | None = None,
        max_handoffs: int = 8,
    ) -> LoopOutcome:
        """Continue one Run until success, control completion, interruption, or failure."""
        current_step_id = active_step_id
        current_usage = usage
        current_compaction = compaction_state
        root_name = agent.name if root_agent_name is None else root_agent_name
        current_handoff_count = handoff_count
        current_max_handoffs = max_handoffs
        result_reader = ReadToolResultTool(memory)
        tools = (*tools, result_reader)
        step_scope: AbstractContextManager[Span] | None = None
        step_span: Span | None = None
        try:
            remaining_seconds = max(0.0, (policy.deadline - datetime.now(UTC)).total_seconds())
            timeout = asyncio.timeout(remaining_seconds)
            async with timeout:
                while True:
                    if policy.deadline <= datetime.now(UTC):
                        await memory.abort(current_step_id)
                        return LoopOutcome(LoopStatus.TIMED_OUT, usage=current_usage)
                    current_usage, remaining_steps, remaining_output_tokens = control.admission(
                        run_id, current_usage, policy
                    )
                    if remaining_steps <= 0 or remaining_output_tokens <= 0:
                        await control.wait_children(run_id)
                        current_usage, remaining_steps, remaining_output_tokens = control.admission(
                            run_id, current_usage, policy
                        )
                    if remaining_steps <= 0 or remaining_output_tokens <= 0:
                        await memory.abort(current_step_id)
                        return LoopOutcome(LoopStatus.LIMIT_REACHED, usage=current_usage)
                    if step_scope is not None and step_span is not None:
                        self.observability.finish(step_span, "succeeded")
                        step_scope.__exit__(None, None, None)
                    step_scope = self.observability.span(
                        STEP_NAME,
                        attributes={
                            RUN_ID: run_id,
                            STEP_ID: current_step_id,
                            AGENT_NAME: agent.name,
                            **({PARENT_RUN_ID: parent_run_id} if parent_run_id is not None else {}),
                        },
                        observation_type="span",
                    )
                    step_span = step_scope.__enter__()
                    if current_usage.total_model_calls >= policy.max_model_steps:
                        await memory.abort(current_step_id)
                        return LoopOutcome(LoopStatus.LIMIT_REACHED, usage=current_usage)
                    active_step = await memory.active()
                    if active_step is None:
                        raise RuntimeError("The active Session step is missing")
                    input_messages = tuple(
                        message for message in active_step.messages if isinstance(message, UserMessage)
                    )
                    control.usage = current_usage
                    if control.pause_requested:
                        await operations.settle()
                        return LoopOutcome(
                            LoopStatus.PAUSED,
                            usage=current_usage,
                            checkpoint=execution_checkpoint(
                                memory,
                                agent,
                                root_name,
                                current_step_id,
                                agent_fingerprint,
                                current_usage,
                                current_compaction,
                                policy,
                                compactor.budget.budget,
                                context_blocks,
                                parent_run_id,
                                current_handoff_count,
                                current_max_handoffs,
                                tuple(control.usage_charges.values()),
                            ),
                        )
                    instructions = build_prompt(
                        agent.prompt,
                        tools=tuple(tool.descriptor for tool in tools),
                        skills=skills,
                    )
                    current_handoffs = tuple(item for item in handoffs if item.source_agent_name == agent.name)
                    model_tools = tuple(tool.model_tool for tool in tools) + handoff_model_tools(current_handoffs)
                    with self.observability.span(
                        CONTEXT_BUILD_NAME,
                        attributes={
                            RUN_ID: run_id,
                            STEP_ID: current_step_id,
                            "pleiad.context.candidate_count": len(context_blocks),
                        },
                    ) as context_span:
                        input_scope = ContextBlock(
                            kind="input_scope",
                            origin=f"run:{run_id}",
                            content=json.dumps(
                                {
                                    "run_id": run_id,
                                    "run_input_ids": memory.snapshot.input_ids,
                                    "step_input_ids": active_step.input_ids,
                                },
                                ensure_ascii=False,
                            ),
                        )
                        context_view = self.context_builder.build((*context_blocks, input_scope))
                        self.observability.finish(context_span, "succeeded")
                    with self.observability.span(
                        MEMORY_READ_NAME,
                        attributes={RUN_ID: run_id, STEP_ID: current_step_id},
                        observation_type="retriever",
                    ) as memory_span:
                        completed_steps = await memory.read()
                        self.observability.finish(memory_span, "succeeded")
                    with self.observability.span(
                        CONTEXT_COMPACT_NAME,
                        attributes={
                            RUN_ID: run_id,
                            STEP_ID: current_step_id,
                            "pleiad.context.step_count": len(completed_steps),
                            "pleiad.context.tool_count": len(model_tools),
                        },
                    ) as compaction_span:
                        compacted = await compactor.compact(
                            completed_steps,
                            instructions=instructions,
                            input_messages=input_messages,
                            tools=model_tools,
                            context_view=context_view,
                            prior_state=current_compaction,
                            summarizer=history_summarizer,
                        )
                        self.observability.finish(compaction_span, "succeeded")
                    current_compaction = compacted.state
                    request_instructions = add_summary(
                        instructions,
                        compacted.state.summary if compacted.state is not None else None,
                    )
                    request_messages = self.context_renderer.render_messages(
                        compacted.messages,
                        input_messages=input_messages,
                        context_view=compacted.context_view,
                    )
                    model_input = build_model_input(
                        instructions=request_instructions,
                        messages=request_messages,
                        tools=model_tools,
                        max_output_tokens=output_token_limit(remaining_output_tokens, agent),
                        output_schema=agent.output_schema,
                    )
                    logger.debug(
                        f"model call started step={current_usage.model_calls + 1} "
                        f"messages={len(request_messages)} tools={len(model_input.tools)}"
                    )
                    try:
                        attempt = await memory.admit_model(
                            attempt_id=uuid4().hex,
                            step_id=current_step_id,
                            provider=model.provider,
                            model=model.model,
                            request_digest=model_input.digest(),
                            max_output_tokens=model_input.max_output_tokens,
                            deadline=policy.deadline,
                            usage=current_usage,
                        )
                    finally:
                        current_usage = memory.snapshot.usage
                        control.usage = current_usage
                    if attempt is None:
                        await abort_safely(memory, "timed_out")
                        return LoopOutcome(LoopStatus.TIMED_OUT, usage=current_usage)
                    await self.emit(
                        ModelStartedEvent(
                            run_id=run_id,
                            step_id=current_step_id,
                            agent_name=agent.name,
                            model=model.model,
                            provider=model.provider,
                            message_count=len(request_messages),
                        ),
                        event_sink,
                    )
                    model_started = self.observability.start_time()
                    model_status = "failed"
                    input_tokens: int | None = None
                    output_tokens: int | None = None
                    try:
                        with self.observability.span(
                            MODEL_GENERATE_NAME,
                            attributes={
                                RUN_ID: run_id,
                                STEP_ID: current_step_id,
                                PROVIDER: model.provider,
                                MODEL: model.model,
                                PROTOCOL: "openai" if isinstance(model, OpenAIModelClient) else "anthropic",
                                OPERATION: "chat",
                            },
                            observation_type="generation",
                        ) as model_span:
                            model_result = await self.receive_model(
                                model,
                                model_input,
                                current_usage.model_calls,
                                event_sink,
                                run_id=run_id,
                                step_id=current_step_id,
                                agent_name=agent.name,
                                model_span=model_span,
                                model_started=model_started,
                                deadline=policy.deadline,
                            )
                            input_tokens = model_result.usage.input_tokens
                            output_tokens = model_result.usage.output_tokens
                            model_status = "succeeded"
                            try:
                                current_usage = await complete_owned(
                                    memory.complete_model(
                                        attempt.attempt_id, model_result.usage, model_result.response_id
                                    )
                                )
                            finally:
                                current_usage = memory.snapshot.usage
                                control.usage = current_usage
                            self.observability.set_attributes(
                                model_span,
                                {
                                    "gen_ai.response.finish_reasons": (model_result.finish_reason.value,),
                                    "gen_ai.usage.input_tokens": input_tokens,
                                    "gen_ai.usage.output_tokens": output_tokens,
                                    "pleiad.model.usage.total_tokens": model_result.usage.total_tokens,
                                    "pleiad.model.usage.reasoning_tokens": model_result.usage.reasoning_tokens,
                                    "pleiad.model.usage.cached_input_tokens": model_result.usage.cached_input_tokens,
                                },
                            )
                            self.observability.finish(model_span, "succeeded")
                            model_status = "succeeded"
                    except asyncio.CancelledError:
                        if model_status != "succeeded":
                            model_status = "cancelled"
                        raise
                    finally:
                        self.observability.metrics.record_model(
                            model.provider,
                            model_status,
                            self.observability.duration(model_started),
                            input_tokens=input_tokens,
                            output_tokens=output_tokens,
                        )
                    logger.debug(
                        f"model call completed step={current_usage.model_calls} "
                        f"tool_calls={len(model_result.tool_calls)}"
                    )
                    current_usage, remaining_steps, remaining_output_tokens = control.admission(
                        run_id, current_usage, policy
                    )
                    await memory.save_usage(current_usage)
                    control.usage = current_usage
                    await self.emit(
                        ModelCompletedEvent(
                            run_id=run_id,
                            step_id=current_step_id,
                            agent_name=agent.name,
                            model=model_result.model,
                            provider=model_result.provider,
                            finish_reason=model_result.finish_reason.value,
                            input_tokens=model_result.usage.input_tokens,
                            output_tokens=model_result.usage.output_tokens,
                        ),
                        event_sink,
                    )
                    assistant = assistant_message(model_result)
                    if not model_result.has_tool_calls:
                        completed = await memory.complete(current_step_id, (assistant,))
                        await self.emit(
                            MessageCompletedEvent(
                                run_id=run_id,
                                step_id=current_step_id,
                                agent_name=agent.name,
                                message=assistant,
                            ),
                            event_sink,
                        )
                        await self.emit(
                            ModelStepCompletedEvent(run_id=run_id, step_id=current_step_id, agent_name=agent.name),
                            event_sink,
                        )
                        if operations.running or await operations.ready_inputs():
                            if operations.running:
                                await operations.wait()
                            next_step = await memory.begin(agent.name, consume_inputs=not control.pause_requested)
                            current_step_id = next_step.step_id
                            await self.emit(
                                ModelStepStartedEvent(run_id=run_id, step_id=current_step_id, agent_name=agent.name),
                                event_sink,
                            )
                            continue
                        output, structured, error = final_output(agent, model_result)
                        if error is not None:
                            return LoopOutcome(
                                LoopStatus.FAILED,
                                final_output=output,
                                usage=current_usage,
                                error=error,
                            )
                        if completed.messages[-1] != assistant:
                            raise RuntimeError("SessionWriter returned a different completed Assistant message")
                        return LoopOutcome(
                            LoopStatus.SUCCEEDED,
                            final_output=output,
                            structured_output=structured,
                            usage=current_usage,
                        )
                    await memory.append(current_step_id, (assistant,))
                    await self.emit(
                        MessageCompletedEvent(
                            run_id=run_id,
                            step_id=current_step_id,
                            agent_name=agent.name,
                            message=assistant,
                        ),
                        event_sink,
                    )
                    handoff_calls = tuple(
                        call
                        for call in model_result.tool_calls
                        if any(item.name == call.name for item in current_handoffs)
                    )
                    if handoff_calls:
                        if len(handoff_calls) != 1 or len(model_result.tool_calls) != 1:
                            conflict = HandoffConflictError()
                            handoff_results = tuple(tool_failure(call.id, conflict) for call in model_result.tool_calls)
                            handoff_binding = None
                        elif not valid_handoff_arguments(handoff_calls[0]):
                            invalid = InvalidToolArgumentsError("Handoff arguments must include a non-empty task.")
                            handoff_results = (tool_failure(handoff_calls[0].id, invalid),)
                            handoff_binding = None
                        elif current_handoff_count >= current_max_handoffs:
                            limit = HandoffLimitError()
                            handoff_results = (tool_failure(handoff_calls[0].id, limit),)
                            handoff_binding = None
                        else:
                            handoff_binding = next(
                                item for item in current_handoffs if item.name == handoff_calls[0].name
                            )
                            await self.emit(
                                HandoffStartedEvent(
                                    run_id=run_id,
                                    source_agent=agent.name,
                                    target_agent=handoff_binding.target.agent.name,
                                    call_id=handoff_calls[0].id,
                                ),
                                event_sink,
                            )
                            handoff_results = (handoff_result(handoff_calls[0], handoff_binding),)
                        current_usage = add_tool_usage(current_usage, len(handoff_results))
                        tool_messages = tuple(
                            tool_result_message(call, result, run_id=run_id, step_id=current_step_id)
                            for call, result in zip(model_result.tool_calls, handoff_results, strict=True)
                        )
                        await memory.complete(
                            current_step_id,
                            tool_messages,
                        )
                        await self.emit_completed_messages(
                            tool_messages, run_id, current_step_id, agent.name, event_sink
                        )
                        await self.emit(
                            ModelStepCompletedEvent(run_id=run_id, step_id=current_step_id, agent_name=agent.name),
                            event_sink,
                        )
                        if current_usage.budget_output_tokens >= policy.max_output_tokens:
                            return LoopOutcome(LoopStatus.LIMIT_REACHED, usage=current_usage)
                        if handoff_binding is None:
                            next_step = await memory.begin(agent.name, consume_inputs=not control.pause_requested)
                            current_step_id = next_step.step_id
                            await self.emit(
                                ModelStepStartedEvent(run_id=run_id, step_id=current_step_id, agent_name=agent.name),
                                event_sink,
                            )
                            continue
                        current_handoff_count += 1
                        handoff_output = handoff_results[0].output
                        if handoff_output is None or not isinstance(handoff_output.get("transition_id"), str):
                            raise RuntimeError("Runtime Handoff result is missing its transition ID")
                        transition_id = handoff_output["transition_id"]
                        await self.emit(
                            HandoffCompletedEvent(
                                run_id=run_id,
                                source_agent=agent.name,
                                target_agent=handoff_binding.target.agent.name,
                                call_id=handoff_calls[0].id,
                                transition_id=transition_id,
                            ),
                            event_sink,
                        )
                        target = handoff_binding.target
                        agent = target.agent
                        model = agent.model
                        tools = (*target.tools, result_reader)
                        skills = target.skills
                        agent_fingerprint = target.fingerprint
                        if handoff_binding.policy is not None:
                            policy = handoff_binding.policy
                            remaining_seconds = max(0.0, (policy.deadline - datetime.now(UTC)).total_seconds())
                            timeout.reschedule(asyncio.get_running_loop().time() + remaining_seconds)
                        next_step = await memory.begin(agent.name, consume_inputs=not control.pause_requested)
                        current_step_id = next_step.step_id
                        await self.emit(
                            ModelStepStartedEvent(run_id=run_id, step_id=current_step_id, agent_name=agent.name),
                            event_sink,
                        )
                        continue
                    tools_by_name = {tool.descriptor.name: tool for tool in tools}
                    outcomes = await self.execute_tools(
                        model_result.tool_calls,
                        tools_by_name,
                        writer=memory,
                        operations=operations,
                        step=current_usage.model_calls,
                        policy=policy,
                        control=control,
                        memory_step_id=current_step_id,
                        assistant_message_id=assistant.message_id,
                        event_sink=event_sink,
                        run_id=run_id,
                        parent_run_id=parent_run_id,
                    )
                    current_usage = control.usage
                    completed_results = tuple(outcome for outcome in outcomes if isinstance(outcome, ToolResult))
                    interruptions = tuple(outcome for outcome in outcomes if isinstance(outcome, ToolInterruption))
                    if interruptions:
                        checkpoint = RunCheckpoint(
                            run_id=run_id,
                            root_agent=root_name,
                            active_agent=agent.name,
                            agent_fingerprint=agent_fingerprint,
                            session_id=memory.session.session_id,
                            writer_epoch=memory.snapshot.writer_epoch,
                            revision=memory.snapshot.revision,
                            step_id=current_step_id,
                            usage=current_usage,
                            usage_charges=tuple(control.usage_charges.values()),
                            interruptions=interruptions,
                            tool_results=completed_results,
                            compaction=current_compaction,
                            deadline=policy.deadline,
                            execution_timeout_seconds=policy.timeout_seconds,
                            context_budget=compactor.budget.budget,
                            max_model_steps=policy.max_model_steps,
                            max_output_tokens=policy.max_output_tokens,
                            tool_concurrency=policy.tool_concurrency,
                            max_handoffs=current_max_handoffs,
                            grants=policy.grants,
                            parent_run_id=parent_run_id,
                            handoff_count=current_handoff_count,
                        )
                        return LoopOutcome(
                            LoopStatus.PAUSED,
                            usage=current_usage,
                            checkpoint=checkpoint,
                            interruptions=interruptions,
                        )
                    tool_messages = tuple(
                        tool_result_message(call, result, run_id=run_id, step_id=current_step_id)
                        for call, result in zip(model_result.tool_calls, completed_results, strict=True)
                    )
                    await memory.complete(
                        current_step_id,
                        tool_messages,
                    )
                    await self.emit_completed_messages(tool_messages, run_id, current_step_id, agent.name, event_sink)
                    await self.emit(
                        ModelStepCompletedEvent(run_id=run_id, step_id=current_step_id, agent_name=agent.name),
                        event_sink,
                    )
                    if (
                        current_usage.total_model_calls >= policy.max_model_steps
                        or current_usage.budget_output_tokens >= policy.max_output_tokens
                    ):
                        return LoopOutcome(LoopStatus.LIMIT_REACHED, usage=current_usage)
                    next_step = await memory.begin(agent.name, consume_inputs=not control.pause_requested)
                    current_step_id = next_step.step_id
                    await self.emit(
                        ModelStepStartedEvent(run_id=run_id, step_id=current_step_id, agent_name=agent.name), event_sink
                    )
        except TimeoutError:
            await abort_safely(memory, "timed_out")
            return LoopOutcome(LoopStatus.TIMED_OUT, usage=current_usage)
        except asyncio.CancelledError:
            await abort_safely(memory, "cancelled")
            raise
        except BusinessError as error:
            await abort_safely(memory, "business_failure")
            return LoopOutcome(LoopStatus.FAILED, usage=current_usage, error=error)
        except Exception:
            await abort_safely(memory, "system_failure")
            raise
        finally:
            if step_scope is not None and step_span is not None:
                active_error_type, active_error, active_traceback = sys.exc_info()
                self.observability.finish(
                    step_span,
                    "failed" if active_error is not None else "completed",
                    error=active_error,
                )
                step_scope.__exit__(active_error_type, active_error, active_traceback)

    async def receive_model(
        self,
        model: OpenAIModelClient | AnthropicModelClient,
        model_input: ModelInput,
        step: int,
        event_sink: EventSink | None,
        *,
        run_id: str,
        step_id: str,
        agent_name: str,
        model_span: Span,
        model_started: float,
        deadline: datetime,
    ) -> ModelResult:
        """Consume one model stream and publish only safe lifecycle metadata."""
        model_result: ModelResult | None = None
        first_output_recorded = False
        message_id = uuid4().hex
        await self.emit(
            MessageStartedEvent(
                run_id=run_id,
                step_id=step_id,
                agent_name=agent_name,
                message_id=message_id,
                role="assistant",
            ),
            event_sink,
        )
        try:
            async for event in stream_model(model, model_input):
                if event.kind is ModelStreamEventKind.TEXT_DELTA and event.text:
                    if not first_output_recorded:
                        self.observability.set_attribute(
                            model_span,
                            FIRST_OUTPUT_MS,
                            round(self.observability.duration(model_started) * 1000),
                        )
                        first_output_recorded = True
                    await self.emit(
                        MessageTextDeltaEvent(
                            run_id=run_id,
                            step_id=step_id,
                            agent_name=agent_name,
                            message_id=message_id,
                            delta=event.text,
                        ),
                        event_sink,
                    )
                elif event.kind is ModelStreamEventKind.REFUSAL_DELTA and event.text:
                    await self.emit(
                        MessageRefusalDeltaEvent(
                            run_id=run_id,
                            step_id=step_id,
                            agent_name=agent_name,
                            message_id=message_id,
                            delta=event.text,
                        ),
                        event_sink,
                    )
                elif event.kind is ModelStreamEventKind.TOOL_CALL_DELTA and event.tool_call_index is not None:
                    await self.emit(
                        MessageToolCallDeltaEvent(
                            run_id=run_id,
                            step_id=step_id,
                            agent_name=agent_name,
                            message_id=message_id,
                            index=event.tool_call_index,
                            id=event.tool_call_id,
                            name=event.tool_name,
                            arguments_delta=event.arguments_delta,
                        ),
                        event_sink,
                    )
                elif event.kind is ModelStreamEventKind.COMPLETED:
                    model_result = event.result
        except asyncio.CancelledError:
            abort_reason = "timed_out" if datetime.now(UTC) >= deadline else "cancelled"
            await self.emit(
                MessageAbortedEvent(
                    run_id=run_id,
                    step_id=step_id,
                    agent_name=agent_name,
                    message_id=message_id,
                    reason=abort_reason,
                ),
                event_sink,
            )
            raise
        except Exception:
            await self.emit(
                ModelFailedEvent(
                    run_id=run_id,
                    step_id=step_id,
                    agent_name=agent_name,
                    model=model.model,
                    provider=model.provider,
                    error_code="runtime.model.stream_failed",
                ),
                event_sink,
            )
            await self.emit(
                MessageAbortedEvent(
                    run_id=run_id,
                    step_id=step_id,
                    agent_name=agent_name,
                    message_id=message_id,
                    reason="failed",
                ),
                event_sink,
            )
            raise
        if model_result is None:
            raise RuntimeError("Model stream ended without a completed result")
        model_result = ModelResult(
            model=model_result.model,
            provider=model_result.provider,
            response_id=model_result.response_id,
            thinking=model_result.thinking,
            message=model_result.message.model_copy(update={"message_id": message_id}),
            structured_output=model_result.structured_output,
            finish_reason=model_result.finish_reason,
            usage=model_result.usage,
        )
        return model_result

    async def execute_tools(
        self,
        calls: tuple[MessageToolCall, ...],
        tools_by_name: dict[str, BaseTool],
        *,
        writer: SessionWriter,
        operations: OperationScope,
        step: int,
        policy: EffectivePolicy,
        control: RunControl,
        memory_step_id: str,
        assistant_message_id: str,
        event_sink: EventSink | None,
        run_id: str,
        parent_run_id: str | None,
    ) -> tuple[ToolExecutionOutcome, ...]:
        """Execute only explicitly parallel-safe Tool calls concurrently."""
        parallel = (
            len(calls) > 1
            and policy.tool_concurrency > 1
            and all(
                (tools_by_name.get(call.name) is not None and tools_by_name[call.name].policy.parallel_safe)
                for call in calls
            )
        )
        logger.debug(f"tool batch started step={step} calls={len(calls)} parallel={parallel}")
        semaphore = asyncio.Semaphore(policy.tool_concurrency)

        async def execute(call: MessageToolCall) -> ToolExecutionOutcome:
            async with semaphore:
                current = control.collect(run_id, control.usage)
                await writer.save_usage(current)
                outcome = await self.execute_tool(
                    call,
                    tools_by_name,
                    writer=writer,
                    operations=operations,
                    step=step,
                    memory_step_id=memory_step_id,
                    assistant_message_id=assistant_message_id,
                    grants=policy.grants,
                    event_sink=event_sink,
                    run_id=run_id,
                    parent_run_id=parent_run_id,
                    parent_deadline=policy.deadline.isoformat(),
                    parent_remaining_model_steps=max(0, policy.max_model_steps - current.total_model_calls),
                    parent_remaining_output_tokens=max(0, policy.max_output_tokens - current.budget_output_tokens),
                    parent_tool_concurrency=policy.tool_concurrency,
                    parent_child_model_calls=current.child_model_calls,
                    parent_child_budget_output_tokens=current.child_budget_output_tokens,
                )
                current = control.collect(run_id, control.usage, outcome.usage_charges)
                if isinstance(outcome, ToolResult):
                    current = control.collect(run_id, add_tool_usage(current, 1))
                await writer.save_usage(current)
                return outcome

        if not parallel:
            outcomes: list[ToolExecutionOutcome] = []
            for call in calls:
                outcomes.append(await execute(call))
                if isinstance(outcomes[-1], ToolInterruption):
                    break
            return tuple(outcomes)

        tasks: list[asyncio.Task[ToolExecutionOutcome]] = []
        async with asyncio.TaskGroup() as task_group:
            for call in calls:
                tasks.append(task_group.create_task(execute(call)))
        return tuple(task.result() for task in tasks)

    async def execute_tool(
        self,
        model_call: MessageToolCall,
        tools_by_name: dict[str, BaseTool],
        *,
        writer: SessionWriter,
        operations: OperationScope,
        step: int,
        memory_step_id: str,
        assistant_message_id: str,
        grants: CapabilityGrants,
        event_sink: EventSink | None,
        run_id: str,
        parent_run_id: str | None,
        parent_deadline: str,
        parent_remaining_model_steps: int,
        parent_remaining_output_tokens: int,
        parent_tool_concurrency: int,
        parent_child_model_calls: int,
        parent_child_budget_output_tokens: int,
    ) -> ToolExecutionOutcome:
        """Validate and execute one Tool call without exposing its arguments to events."""
        tool = tools_by_name.get(model_call.name)
        tool_code = tool.descriptor.code if tool is not None else f"unknown:{model_call.name}"
        binding = (
            {"code": tool_code}
            if tool is None
            else {
                "descriptor": tool.descriptor.model_dump(mode="json"),
                "schema": tool.input_schema,
                "policy": tool.policy.model_dump(mode="json"),
            }
        )
        intent = ToolCallRecord(
            run_id=run_id,
            step_id=memory_step_id,
            call_id=model_call.id,
            tool_code=tool_code,
            binding_digest=hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest(),
            arguments_digest=hashlib.sha256(model_call.arguments.encode()).hexdigest(),
            authorization_ref=hashlib.sha256(json.dumps(sorted(grants.capabilities)).encode()).hexdigest(),
        )
        await writer.save_tool_call(intent)
        await self.emit(
            ToolStartedEvent(
                run_id=run_id,
                step_id=memory_step_id,
                assistant_message_id=assistant_message_id,
                tool_code=tool_code,
                call_id=model_call.id,
            ),
            event_sink,
        )
        tool_started = self.observability.start_time()
        with (
            self.observability.span(
                TOOL_EXECUTE_NAME,
                attributes={
                    RUN_ID: run_id,
                    STEP_ID: memory_step_id,
                    TOOL_CODE: tool_code,
                    TOOL_NAME: model_call.name,
                    CALL_ID: model_call.id,
                },
                observation_type="tool",
            ) as tool_span,
            bind_log_context(call_id=model_call.id, tool_code=tool_code),
        ):
            if tool is None:
                outcome: ToolExecutionOutcome = tool_failure(
                    model_call.id, UnknownToolError("The requested Tool is not available.")
                )
            elif parse_tool_arguments(model_call.arguments) is None:
                outcome = tool_failure(
                    model_call.id, InvalidToolArgumentsError("Tool arguments are not a JSON object.")
                )
            else:
                context = ToolContext(
                    call_id=model_call.id,
                    run_id=run_id,
                    parent_run_id=parent_run_id,
                    step_id=memory_step_id,
                    grants=grants,
                    parent_limits=ParentRunLimits(
                        deadline=datetime.fromisoformat(parent_deadline),
                        remaining_model_steps=parent_remaining_model_steps,
                        remaining_output_tokens=parent_remaining_output_tokens,
                        tool_concurrency=parent_tool_concurrency,
                        child_model_calls=parent_child_model_calls,
                        child_budget_output_tokens=parent_child_budget_output_tokens,
                    ),
                )
                call = ToolCall(
                    call_id=model_call.id,
                    tool_code=tool.descriptor.code,
                    arguments=parse_tool_arguments(model_call.arguments) or {},
                )
                await writer.save_tool_call(intent.model_copy(update={"state": "running"}))
                try:
                    if tool.policy.background:
                        prepared = await self.tool_runtime.prepare(tool, call, context)
                        outcome = await operations.start(tool, call, context) if prepared is None else prepared
                    else:
                        outcome = await self.tool_runtime.execute(tool, call, context)
                except (asyncio.CancelledError, Exception):
                    intents = await writer.store.tool_calls(writer.session_id, writer.run_id)
                    latest = next(value for value in intents if value.call_id == call.call_id)
                    if latest.state not in {"succeeded", "failed", "cancelled"}:
                        await writer.save_tool_call(latest.model_copy(update={"state": "unknown"}))
                    raise
            tool_error_code = outcome.error_code if isinstance(outcome, ToolResult) else None
            tool_error_type = (
                outcome.error_type.value if isinstance(outcome, ToolResult) and outcome.error_type is not None else None
            )
            tool_status = "succeeded" if isinstance(outcome, ToolInterruption) or outcome.ok else "failed"
            self.observability.finish(
                tool_span,
                tool_status,
                error_code=tool_error_code,
                error_type=tool_error_type,
            )
        self.observability.metrics.record_tool(
            tool_status,
            self.observability.duration(tool_started),
            failed=tool_status != "succeeded",
        )
        if isinstance(outcome, ToolResult):
            intents = await writer.store.tool_calls(writer.session_id, writer.run_id)
            latest = next(value for value in intents if value.call_id == intent.call_id)
            if latest.operation_id is None:
                await writer.save_tool_call(
                    latest.model_copy(update={"state": "succeeded" if outcome.ok else "failed", "result": outcome})
                )
        if isinstance(outcome, ToolInterruption):
            await writer.save_tool_call(intent.model_copy(update={"state": "prepared"}))
            await self.emit(
                ToolInterruptedEvent(
                    run_id=run_id,
                    step_id=memory_step_id,
                    assistant_message_id=assistant_message_id,
                    tool_code=tool_code,
                    call_id=model_call.id,
                    interruption_id=outcome.interruption_id,
                ),
                event_sink,
            )
            return outcome
        await self.emit(
            ToolCompletedEvent(
                run_id=run_id,
                step_id=memory_step_id,
                assistant_message_id=assistant_message_id,
                tool_code=tool_code,
                call_id=model_call.id,
                result_message_id=tool_result_message(
                    model_call,
                    outcome,
                    run_id=run_id,
                    step_id=memory_step_id,
                ).message_id,
                ok=outcome.ok,
            ),
            event_sink,
        )
        if not outcome.ok:
            logger.warning(f"tool business failure call_id={model_call.id} code={outcome.error_code}")
        return outcome

    async def emit(self, event: Event, event_sink: EventSink | None) -> None:
        """Publish one event through the configured delivery path."""
        if event_sink is None:
            return
        await event_sink(event)

    async def emit_completed_messages(
        self,
        messages: tuple[ToolMessage, ...],
        run_id: str,
        step_id: str,
        agent_name: str,
        event_sink: EventSink | None,
    ) -> None:
        """Publish Tool message snapshots after Memory accepts them."""
        for message in messages:
            await self.emit(
                MessageCompletedEvent(
                    run_id=run_id,
                    step_id=step_id,
                    agent_name=agent_name,
                    message=message,
                ),
                event_sink,
            )


def add_tool_usage(usage: RunUsage, count: int) -> RunUsage:
    """Add completed Tool calls without changing usage or budget accounting."""
    return usage.model_copy(update={"tool_calls": usage.tool_calls + count})


def assistant_message(model_result: ModelResult) -> AssistantMessage:
    """Project the replayable visible model response into SessionWriter."""
    return model_result.message


def tool_failure(call_id: str, error: BusinessError) -> ToolResult:
    """Project a Tool business failure without copying untrusted arguments."""
    now = datetime.now(UTC)
    return ToolResult(
        call_id=call_id,
        ok=False,
        error_code=error.code,
        error_message=error.message,
        error_type=error.type,
        started_at=now,
        finished_at=now,
    )


def parse_tool_arguments(arguments: str) -> JsonObject | None:
    """Parse model Tool arguments at the execution boundary."""
    try:
        value: object = json.loads(arguments)
    except json.JSONDecodeError:
        return None
    if not isinstance(value, dict):
        return None
    return thaw_json_object(value)


def handoff_model_tools(bindings: tuple[RuntimeHandoffBinding, ...]) -> tuple[Tool, ...]:
    """Project explicit Handoff controls into the current model request."""
    return tuple(
        Tool(
            name=binding.name,
            description=binding.description,
            input_schema={
                "type": "object",
                "properties": {"task": {"type": "string", "minLength": 1}},
                "required": ["task"],
                "additionalProperties": False,
            },
        )
        for binding in bindings
    )


def handoff_result(model_call: MessageToolCall, binding: RuntimeHandoffBinding) -> ToolResult:
    """Record a Handoff control transition without executing it as a normal Tool."""
    now = datetime.now(UTC)
    return ToolResult(
        call_id=model_call.id,
        ok=True,
        output={
            "control": "handoff",
            "target_agent": binding.target.agent.name,
            "transition_id": uuid4().hex,
        },
        started_at=now,
        finished_at=now,
    )


def valid_handoff_arguments(model_call: MessageToolCall) -> bool:
    """Require the fixed Handoff input contract before switching control."""
    arguments = parse_tool_arguments(model_call.arguments)
    if arguments is None:
        return False
    task = arguments.get("task")
    return isinstance(task, str) and bool(task.strip())


def final_output(agent: Agent, model_result: ModelResult) -> tuple[str, JsonObject | None, BusinessError | None]:
    """Validate a direct model response against the Agent output schema."""
    if agent.output_schema is None:
        structured = model_result.structured_output if isinstance(model_result.structured_output, dict) else None
        return model_result.text, structured, None
    try:
        candidate: object = model_result.structured_output
        if candidate is None:
            candidate = json.loads(model_result.text)
        if not isinstance(candidate, dict):
            raise ValueError("structured Agent output must be an object")
        errors = tuple(Draft202012Validator(thaw_json_object(agent.output_schema)).iter_errors(candidate))
        if errors:
            raise ValueError("structured Agent output does not match its schema")
        return model_result.text, candidate, None
    except (TypeError, ValueError):
        return model_result.text, None, InvalidAgentOutputError()


async def abort_safely(memory: SessionWriter, reason: str) -> None:
    """Abort only the actual active step while retaining completed response facts."""
    try:
        active = await memory.active()
        if active is not None:
            await memory.abort(active.step_id)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.error(f"memory step cleanup failed reason={reason}", exc_info=True)


def output_token_limit(policy_limit: int, agent: Agent) -> int:
    """Return the tighter Runtime and Agent output limit."""
    agent_limit = agent.policy_hints.max_output_tokens
    if agent_limit is None:
        return policy_limit
    return min(policy_limit, agent_limit)


def execution_checkpoint(
    writer: SessionWriter,
    agent: Agent,
    root_agent: str,
    step_id: str,
    fingerprint: str,
    usage: RunUsage,
    compaction: CompactionState | None,
    policy: EffectivePolicy,
    context_budget: ContextBudget,
    context: tuple[ContextBlock, ...],
    parent_run_id: str | None,
    handoff_count: int,
    max_handoffs: int,
    usage_charges: tuple[UsageCharge, ...],
) -> RunCheckpoint:
    """Capture an execution-safe position using the current fenced writer."""
    return RunCheckpoint(
        session_id=writer.session.session_id,
        run_id=writer.run_id,
        writer_epoch=writer.snapshot.writer_epoch,
        revision=writer.snapshot.revision,
        root_agent=root_agent,
        active_agent=agent.name,
        agent_fingerprint=fingerprint,
        step_id=step_id,
        deadline=policy.deadline,
        max_model_steps=policy.max_model_steps,
        max_output_tokens=policy.max_output_tokens,
        tool_concurrency=policy.tool_concurrency,
        max_handoffs=max_handoffs,
        grants=policy.grants,
        usage=usage,
        usage_charges=usage_charges,
        compaction=compaction,
        parent_run_id=parent_run_id,
        handoff_count=handoff_count,
        context=context,
        execution_timeout_seconds=policy.timeout_seconds,
        context_budget=context_budget,
    )
