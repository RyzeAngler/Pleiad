import asyncio
import hashlib
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from time import monotonic
from typing import Literal

from jsonschema import Draft202012Validator

from pleiad.agent.agent import Agent
from pleiad.agent.errors import AgentConfigurationError
from pleiad.budgets import BudgetLedger
from pleiad.context.budget import BudgetGuard
from pleiad.context.compaction.compactor import ContextCompactor, HistorySummarizer
from pleiad.context.selection.models import ContextBlock
from pleiad.contracts import CapabilityGrants, JsonObject, JsonValue, thaw_json_object
from pleiad.errors.base import PleiadError
from pleiad.events.contracts import Event, EventSink
from pleiad.events.message import MessageCompletedEvent
from pleiad.events.runtime import (
    ModelStepCompletedEvent,
    ModelStepStartedEvent,
    RunCancelledEvent,
    RunCompletedEvent,
    RunFailedEvent,
    RunPausedEvent,
    RunResumedEvent,
    RunStartedEvent,
    ToolCompletedEvent,
)
from pleiad.logging.context import bind_log_context
from pleiad.messages.message import AssistantMessage
from pleiad.messages.message import ToolCall as MessageToolCall
from pleiad.observability.attributes import AGENT_NAME, PARENT_RUN_ID, RUN_ID, RUN_NAME
from pleiad.observability.tracing import Observability
from pleiad.runtime.checkpoint import ResumeDecision
from pleiad.runtime.errors import RuntimeStateError
from pleiad.runtime.execution.control import RuntimeAgentBinding, RuntimeHandoffBinding
from pleiad.runtime.execution.control_state import RunControl
from pleiad.runtime.execution.loop import AgentLoop, add_tool_usage
from pleiad.runtime.execution.model_input import tool_result_message
from pleiad.runtime.execution.outcome import LoopOutcome, LoopStatus
from pleiad.runtime.execution.read_tool_result import ReadToolResultTool
from pleiad.runtime.handle import RunHandle
from pleiad.runtime.limits import RunLimits
from pleiad.runtime.operations import OperationScope
from pleiad.runtime.policy import EffectivePolicy, HostPolicy, effective_policy
from pleiad.runtime.result import RunOutcome, RunResult, RunSuspension
from pleiad.sessions.checkpoint import RunCheckpoint
from pleiad.sessions.model_step import ModelStep
from pleiad.sessions.records import RunReceipt, RunSnapshot, RunStatus
from pleiad.sessions.recovery import ActionEvidence, ReconcileResult
from pleiad.sessions.session import Session
from pleiad.sessions.usage import RunUsage
from pleiad.sessions.writer import SessionWriter
from pleiad.skills.contracts import Skill
from pleiad.skills.loader import SkillLoader
from pleiad.tools.base import BaseTool, ToolScope
from pleiad.tools.builtins.read_file import ReadFileTool
from pleiad.tools.contracts import ParentRunLimits, ToolCall, ToolContext, ToolResult
from pleiad.tools.interruption import InterruptionResponse, ToolAuthorization, ToolExecutionOutcome, ToolInterruption
from pleiad.tools.registry import ToolRegistry
from pleiad.tools.runtime import ToolRuntime

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ResumeProgress:
    """Keep resolved action facts and a possible stop disposition together."""

    step: ModelStep
    checkpoint: RunCheckpoint
    outcome: LoopOutcome | None = None


class Runner:
    """Advance persistent Sessions through one owned execution mechanism."""

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
        """Bind reusable runtime mechanisms and host-imposed upper limits."""
        self.tools = tools
        self.skills = skills
        self.tool_runtime = tool_runtime
        self.event_sink = event_sink
        self.host_policy = HostPolicy() if host_policy is None else host_policy
        self.observability = Observability() if observability is None else observability
        self.loop = AgentLoop(tool_runtime, observability=self.observability) if loop is None else loop
        self.history_summarizer = history_summarizer
        self.handles: dict[tuple[int, str, str], RunHandle] = {}
        self.lock = asyncio.Lock()

    async def start(
        self,
        agent: Agent,
        *,
        session: Session,
        invocation_id: str,
        input_ids: tuple[str, ...],
        input_mode: Literal["batch", "live"],
        limits: RunLimits,
        context: tuple[ContextBlock, ...] = (),
        handoffs: tuple[RuntimeHandoffBinding, ...] = (),
        parent_run_id: str | None = None,
        parent_policy: EffectivePolicy | None = None,
    ) -> RunHandle:
        """Atomically accept an invocation and own its background execution.

        Args:
            agent: Definition whose capabilities are bound before admission.
            session: Persistent interaction and input owner.
            invocation_id: Stable host request key reused after a lost acknowledgement.
            input_ids: Ordered, accepted inputs to claim for this invocation.
            input_mode: Fixed external batch or live safe-boundary consumption.
            limits: Request limits intersected with the host, Agent, and parent.
            context: Immutable request-local references.
            handoffs: Explicit same-Run Agent transitions.
            parent_run_id: Owning SDK invocation for scoped delegation.
            parent_policy: Current restrictions inherited from that owner.

        Returns:
            The same live owner handle for an identical local retry, or a receipt handle.
        """
        binding = self.resolve_binding(agent)
        policy = effective_policy(limits, self.host_policy, agent_hints=agent.policy_hints, parent=parent_policy)
        prepared = self.prepare_handoffs(handoffs, policy)
        payload = {
            "agent": binding.fingerprint,
            "inputs": list(input_ids),
            "mode": input_mode,
            "limits": limits.model_dump(mode="json"),
            "context": [block.model_dump(mode="json") for block in context],
            "handoffs": [item.target.fingerprint for item in prepared],
            "parent_run_id": parent_run_id,
            "parent_policy": None
            if parent_policy is None
            else {
                "timeout_seconds": parent_policy.timeout_seconds,
                "deadline": parent_policy.deadline.isoformat(),
                "max_model_steps": parent_policy.max_model_steps,
                "max_output_tokens": parent_policy.max_output_tokens,
                "tool_concurrency": parent_policy.tool_concurrency,
                "grants": {
                    "capabilities": sorted(parent_policy.grants.capabilities),
                    "resource_read": sorted(parent_policy.grants.resource_read),
                    "resource_write": sorted(parent_policy.grants.resource_write),
                },
            },
        }
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        ).hexdigest()
        key = (id(session.store), session.session_id, invocation_id)
        async with self.lock:
            accepted = await session.store.start(
                session.session_id,
                invocation_id=invocation_id,
                parameters_digest=digest,
                input_ids=input_ids,
                input_mode=input_mode,
                agent_id=agent.name,
                parent_run_id=parent_run_id,
            )
            snapshot = accepted.snapshot
            existing = self.handles.get(key)
            if existing is not None:
                return existing
            if accepted.duplicate and snapshot.state in {RunStatus.RUNNING, RunStatus.WAITING_EVENT}:
                raise RuntimeStateError("The accepted Run has another execution owner; read or reconcile its snapshot.")
            handle = self.create_handle(session, snapshot)
            self.handles[key] = handle
            if handle.outcome is None:
                writer = SessionWriter(session, snapshot, observability=self.observability)
                handle.launch(
                    lambda: self.execute(
                        agent, binding, writer, handle, limits, policy, context, prepared, parent_run_id, None, ()
                    )
                )
            return handle

    async def resume(
        self,
        agent: Agent,
        *,
        session: Session,
        checkpoint: RunCheckpoint,
        decisions: tuple[ResumeDecision, ...] = (),
        limits: RunLimits,
        handoffs: tuple[RuntimeHandoffBinding, ...] = (),
        parent_policy: EffectivePolicy | None = None,
    ) -> RunHandle:
        """Resume the same Run after validating its exact stored checkpoint."""
        if checkpoint.session_id != session.session_id or checkpoint.root_agent != agent.name:
            raise RuntimeStateError("The checkpoint does not belong to this Agent and Session.")
        if checkpoint.parent_run_id is not None and parent_policy is None:
            raise RuntimeStateError("Resuming a child Run requires its current parent policy.")
        elapsed = checkpoint.execution_seconds_used
        allowance = min(limits.execution_timeout_seconds, checkpoint.execution_timeout_seconds)
        remaining = max(0.000001, allowance - elapsed)
        deadlines = tuple(value for value in (limits.deadline, checkpoint.absolute_deadline) if value is not None)
        tightened = limits.model_copy(
            update={
                "execution_timeout_seconds": remaining,
                "deadline": min(deadlines) if deadlines else None,
                "max_model_steps": min(limits.max_model_steps, checkpoint.max_model_steps),
                "max_output_tokens": min(limits.max_output_tokens, checkpoint.max_output_tokens),
                "tool_concurrency": min(limits.tool_concurrency, checkpoint.tool_concurrency),
                "max_handoffs": min(limits.max_handoffs, checkpoint.max_handoffs),
                "grants": checkpoint.grants if limits.grants is None else checkpoint.grants.intersect(limits.grants),
            }
        )
        if elapsed >= allowance:
            tightened = tightened.model_copy(update={"deadline": datetime.now(UTC)})
        policy = effective_policy(tightened, self.host_policy, agent_hints=agent.policy_hints, parent=parent_policy)
        prepared = self.prepare_handoffs(handoffs, policy)
        bindings = {agent.name: self.resolve_binding(agent)}
        bindings.update({item.target.agent.name: item.target for item in prepared})
        binding = bindings.get(checkpoint.active_agent)
        if binding is None or binding.fingerprint != checkpoint.agent_fingerprint:
            raise RuntimeStateError("The checkpoint Agent binding changed before resume.")
        validate_resolutions(checkpoint.interruptions, decisions)
        await self.validate_pending(binding, session, checkpoint, policy)
        async with self.lock:
            snapshot = await session.store.resume(session.session_id, checkpoint)
            writer = SessionWriter(session, snapshot, observability=self.observability)
            handle = self.create_handle(session, snapshot, restore_outcome=False)
            self.handles[(id(session.store), session.session_id, snapshot.invocation_id)] = handle
            handle.launch(
                lambda: self.execute(
                    agent,
                    binding,
                    writer,
                    handle,
                    limits,
                    policy,
                    checkpoint.context,
                    prepared,
                    checkpoint.parent_run_id,
                    checkpoint,
                    decisions,
                )
            )
            return handle

    async def validate_pending(
        self, binding: RuntimeAgentBinding, session: Session, checkpoint: RunCheckpoint, policy: EffectivePolicy
    ) -> None:
        """Reject changed pending-action authority before taking the paused writer."""
        active = await session.store.active(session.session_id, checkpoint.run_id)
        if active is None:
            if checkpoint.step_id is not None or checkpoint.interruptions:
                raise RuntimeStateError("The persisted checkpoint step is missing.")
            return
        if active.step_id != checkpoint.step_id:
            raise RuntimeStateError("The persisted step does not match its checkpoint.")
        if not checkpoint.interruptions:
            return
        assistant = next((value for value in active.messages if isinstance(value, AssistantMessage)), None)
        if assistant is None:
            raise RuntimeStateError("Pending tool declarations are missing from the checkpoint.")
        tools = {tool.descriptor.code: tool for tool in binding.tools}
        for interruption in checkpoint.interruptions:
            tool = tools.get(interruption.tool_code)
            call = next((value for value in assistant.tool_calls if value.id == interruption.call_id), None)
            if tool is None or call is None or call.name != interruption.tool_name:
                raise RuntimeStateError("The pending tool binding changed before resume.")
            if parse_tool_arguments(call.arguments) != interruption.arguments:
                raise RuntimeStateError("The pending tool arguments changed before resume.")
            if not Draft202012Validator(tool.input_schema).is_valid(interruption.arguments):
                raise RuntimeStateError("The pending tool schema changed before resume.")
            if tool.required_permissions - policy.grants.capabilities:
                raise RuntimeStateError("Current permissions do not grant the pending tool action.")

    def create_handle(self, session: Session, snapshot: RunSnapshot, *, restore_outcome: bool = True) -> RunHandle:
        """Bind a local scope or expose a previously committed stable outcome."""
        outcome: RunOutcome | None = None
        if restore_outcome and snapshot.receipt is not None:
            outcome = RunResult(snapshot.receipt)
        elif restore_outcome and snapshot.checkpoint is not None and snapshot.state is RunStatus.PAUSED:
            outcome = RunSuspension(snapshot.checkpoint)

        async def cancel_paused(reason: str) -> RunResult:
            checkpoint = await session.checkpoint(snapshot.run_id)
            if checkpoint is None:
                current = await session.store.run(session.session_id, snapshot.run_id)
                if current.receipt is None:
                    raise RuntimeStateError("The paused Run no longer has a checkpoint or receipt.")
                return RunResult(current.receipt)
            return await self.cancel_paused(session=session, checkpoint=checkpoint, reason=reason)

        return RunHandle(session, snapshot.run_id, RunControl(), cancel_paused, outcome)

    def bind_scopes(
        self, binding: RuntimeAgentBinding, handoffs: tuple[RuntimeHandoffBinding, ...]
    ) -> tuple[BudgetLedger | None, tuple[tuple[str, ToolScope], ...]]:
        """Resolve actual shared child admission and resource owners across Handoffs."""
        tools = (*binding.tools, *(tool for handoff in handoffs for tool in handoff.target.tools))
        ledgers = {id(tool.budget_ledger): tool.budget_ledger for tool in tools if tool.budget_ledger is not None}
        if len(ledgers) > 1:
            raise AgentConfigurationError("Scoped Tools in one Run must share one BudgetLedger.")
        scopes: list[tuple[str, ToolScope]] = []
        for tool in tools:
            scope = tool.execution_scope
            if scope is not None and not any(value is scope for code, value in scopes):
                scopes.append((tool.descriptor.code, scope))
        return next(iter(ledgers.values()), None), tuple(scopes)

    async def open_scopes(
        self,
        control: RunControl,
        binding: RuntimeAgentBinding,
        handoffs: tuple[RuntimeHandoffBinding, ...],
        run_id: str,
        checkpoint: RunCheckpoint | None,
        policy: EffectivePolicy,
    ) -> None:
        """Restore and enter the actual child scopes before a Run can admit actions."""
        control.budget_ledger, control.scopes = self.bind_scopes(binding, handoffs)
        if checkpoint is not None:
            control.usage_charges = {value.charge_id: value for value in checkpoint.usage_charges}
            if checkpoint.budget is not None:
                if control.budget_ledger is None:
                    raise RuntimeStateError("The checkpoint's child budget owner is unavailable.")
                control.budget_ledger.restore_checkpoint(checkpoint.budget)
            keys = {code for code, scope in control.scopes}
            if not set(checkpoint.tool_scope_states) <= keys:
                raise RuntimeStateError("The checkpoint's child scope binding changed.")
            for code, scope in control.scopes:
                value = checkpoint.tool_scope_states.get(code)
                if value is not None:
                    scope.restore(run_id, value)
        control.admission(run_id, control.usage, policy)
        for scope_binding in control.scopes:
            await scope_binding[1].open(run_id)

    async def commit_failure(
        self, writer: SessionWriter, control: RunControl, error: Exception, publish: EventSink
    ) -> None:
        """Persist a failed owner after resource cleanup without losing its original diagnostic cause."""
        receipt = RunReceipt(
            run_id=writer.run_id,
            input_ids=writer.snapshot.input_ids,
            state="failed",
            reason="system_failure",
            error_code=error.code if isinstance(error, PleiadError) else "runtime.system_failure",
            error_message=str(error),
            usage=control.usage,
            completed_at=datetime.now(UTC),
        )
        await writer.finish(receipt)
        await publish(
            RunFailedEvent(
                run_id=writer.run_id,
                error_code=receipt.error_code or "runtime.system_failure",
                error_message="The Run stopped because of an internal failure.",
            )
        )
        logger.error(f"run failed run={writer.run_id} code={receipt.error_code}", exc_info=True)

    def scope_checkpoint(self, checkpoint: RunCheckpoint, control: RunControl) -> RunCheckpoint:
        """Persist cumulative charges and every live ownership snapshot at one safe boundary."""
        states: dict[str, JsonObject] = {}
        for code, scope in control.scopes:
            value = scope.checkpoint(checkpoint.run_id)
            if value is not None:
                states[code] = value
        ledger = control.budget_ledger
        budget = (
            None
            if ledger is None
            else ledger.checkpoint(
                checkpoint.run_id,
                tuple(key for key, value in ledger.reservations.items() if value.parent_run_id == checkpoint.run_id),
            )
        )
        return checkpoint.model_copy(
            update={
                "usage": control.collect(checkpoint.run_id, checkpoint.usage),
                "usage_charges": tuple(control.usage_charges.values()),
                "budget": budget,
                "tool_scope_states": states,
            }
        )

    async def execute(
        self,
        root_agent: Agent,
        binding: RuntimeAgentBinding,
        writer: SessionWriter,
        handle: RunHandle,
        limits: RunLimits,
        policy: EffectivePolicy,
        context: tuple[ContextBlock, ...],
        handoffs: tuple[RuntimeHandoffBinding, ...],
        parent_run_id: str | None,
        checkpoint: RunCheckpoint | None,
        decisions: tuple[ResumeDecision, ...],
    ) -> RunOutcome:
        """Own execution, cleanup, and the durable outcome commit in one scope."""
        with (
            self.observability.span(
                RUN_NAME,
                attributes={
                    RUN_ID: writer.run_id,
                    AGENT_NAME: root_agent.name,
                    **({PARENT_RUN_ID: parent_run_id} if parent_run_id is not None else {}),
                },
                observation_type="agent",
            ) as run_span,
            bind_log_context(run_id=writer.run_id, session_id=writer.session_id),
        ):
            try:
                started = monotonic()
                prior_seconds = 0.0 if checkpoint is None else checkpoint.execution_seconds_used
                usage = RunUsage() if checkpoint is None else checkpoint.usage
                handle.control.usage = usage

                async def publish(event: Event) -> None:
                    await writer.emit(event)
                    await handle.notify()
                    if self.event_sink is not None:
                        try:
                            await self.event_sink(event)
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            logger.error(f"run observer failed kind={event.kind}", exc_info=True)

                stopped: LoopOutcome | None = None
                try:
                    async with OperationScope(writer, self.tool_runtime, policy.tool_concurrency) as operations:
                        await self.open_scopes(handle.control, binding, handoffs, writer.run_id, checkpoint, policy)
                        if checkpoint is None:
                            active = await writer.begin(binding.agent.name)
                            await publish(RunStartedEvent(run_id=writer.run_id, agent_name=binding.agent.name))
                        else:
                            await publish(RunResumedEvent(run_id=writer.run_id, agent_name=binding.agent.name))
                            progress = await self.resolve_checkpoint(
                                binding, writer, checkpoint, decisions, policy, publish, handle.control, operations
                            )
                            active = progress.step
                            pending = progress.checkpoint.interruptions
                            tool_results = progress.checkpoint.tool_results
                            usage = progress.checkpoint.usage
                            handle.control.usage = usage
                            stopped = progress.outcome
                            if pending and stopped is None:
                                changed = checkpoint.model_copy(
                                    update={
                                        "interruptions": pending,
                                        "tool_results": tool_results,
                                        "usage": usage,
                                        "writer_epoch": writer.snapshot.writer_epoch,
                                        "revision": writer.snapshot.revision,
                                        "execution_seconds_used": prior_seconds + monotonic() - started,
                                    }
                                )
                                stopped = LoopOutcome(
                                    LoopStatus.PAUSED, usage=usage, checkpoint=changed, interruptions=pending
                                )
                        if stopped is not None:
                            outcome = stopped
                        else:
                            await publish(
                                ModelStepStartedEvent(
                                    run_id=writer.run_id, step_id=active.step_id, agent_name=binding.agent.name
                                )
                            )
                            for message in active.messages:
                                await publish(
                                    MessageCompletedEvent(
                                        run_id=writer.run_id,
                                        step_id=active.step_id,
                                        agent_name=binding.agent.name,
                                        message=message,
                                    )
                                )
                            outcome = await self.loop.run(
                                binding.agent,
                                run_id=writer.run_id,
                                memory=writer,
                                active_step_id=active.step_id,
                                control=handle.control,
                                operations=operations,
                                model=binding.agent.model,
                                tools=binding.tools,
                                skills=binding.skills,
                                policy=policy,
                                compactor=ContextCompactor(
                                    BudgetGuard(limits.context_budget), self.loop.context_renderer
                                ),
                                context_blocks=context,
                                agent_fingerprint=binding.fingerprint,
                                usage=handle.control.usage,
                                compaction_state=None if checkpoint is None else checkpoint.compaction,
                                event_sink=publish,
                                history_summarizer=self.history_summarizer,
                                root_agent_name=root_agent.name,
                                handoffs=handoffs,
                                handoff_count=0 if checkpoint is None else checkpoint.handoff_count,
                                parent_run_id=parent_run_id,
                                max_handoffs=limits.max_handoffs,
                            )
                        if outcome.status is LoopStatus.PAUSED:
                            await operations.settle()
                    await handle.control.close_scopes(
                        writer.run_id, cancelled=False, suspended=outcome.status is LoopStatus.PAUSED
                    )
                    outcome = replace(outcome, usage=handle.control.collect(writer.run_id, outcome.usage))
                    if monotonic() - started >= policy.timeout_seconds or datetime.now(UTC) >= policy.deadline:
                        await handle.control.close_scopes(writer.run_id, cancelled=True, suspended=False)
                        outcome = replace(outcome, status=LoopStatus.TIMED_OUT)
                    if outcome.status is LoopStatus.PAUSED:
                        if outcome.checkpoint is None:
                            raise RuntimeStateError("A paused execution did not produce a safe checkpoint.")
                        saved_checkpoint = outcome.checkpoint.model_copy(
                            update={
                                "execution_seconds_used": prior_seconds + monotonic() - started,
                                "execution_timeout_seconds": min(
                                    limits.execution_timeout_seconds,
                                    limits.execution_timeout_seconds
                                    if checkpoint is None
                                    else checkpoint.execution_timeout_seconds,
                                ),
                                "absolute_deadline": limits.deadline
                                if checkpoint is None
                                else checkpoint.absolute_deadline,
                                "context": context,
                            }
                        )
                        saved = await writer.save_checkpoint(self.scope_checkpoint(saved_checkpoint, handle.control))
                        await publish(
                            RunPausedEvent(
                                run_id=writer.run_id,
                                interruption_ids=tuple(item.interruption_id for item in saved.interruptions),
                            )
                        )
                        # Read the exact checkpoint after notification persistence.
                        committed_checkpoint = await writer.session.checkpoint(writer.run_id)
                        if committed_checkpoint is None:
                            raise RuntimeStateError("The committed checkpoint is missing.")
                        return RunSuspension(committed_checkpoint)
                    state = RunStatus.SUCCEEDED if outcome.status is LoopStatus.SUCCEEDED else RunStatus.FAILED
                    receipt = RunReceipt(
                        run_id=writer.run_id,
                        input_ids=writer.snapshot.input_ids,
                        state=state.value,
                        reason=outcome.status.value,
                        final_output=outcome.final_output,
                        structured_output=outcome.structured_output,
                        usage=outcome.usage,
                        error_code=None if outcome.error is None else outcome.error.code,
                        error_message=None if outcome.error is None else outcome.error.message,
                        completed_at=datetime.now(UTC),
                    )
                    await self.abort_active(writer)
                    await writer.finish(receipt)
                    event = (
                        RunCompletedEvent(run_id=writer.run_id)
                        if state is RunStatus.SUCCEEDED
                        else RunFailedEvent(
                            run_id=writer.run_id,
                            error_code=receipt.error_code or receipt.reason or "runtime.failed",
                            error_message=receipt.error_message or "The Run did not satisfy its completion contract.",
                        )
                    )
                    await publish(event)
                    committed = writer.snapshot.receipt
                    if committed is None:
                        raise RuntimeStateError("The committed Run receipt is missing.")
                    return RunResult(committed, outcome.error)
                except asyncio.CancelledError as error:
                    await self.abort_active(writer)
                    prior_receipt = writer.snapshot.receipt
                    if prior_receipt is not None:
                        raise
                    try:
                        await handle.control.close_scopes(writer.run_id, cancelled=True, suspended=False)
                    except Exception as cleanup_error:
                        await self.commit_failure(writer, handle.control, cleanup_error, publish)
                        raise error from cleanup_error
                    receipt = RunReceipt(
                        run_id=writer.run_id,
                        input_ids=writer.snapshot.input_ids,
                        state="cancelled",
                        reason=handle.control.cancel_reason or "owner_cancelled",
                        usage=handle.control.usage,
                        completed_at=datetime.now(UTC),
                    )
                    await writer.finish(receipt)
                    await publish(RunCancelledEvent(run_id=writer.run_id))
                    committed = writer.snapshot.receipt
                    if committed is None:
                        raise RuntimeStateError("The cancellation receipt is missing.") from error
                    raise
                except Exception as error:
                    await self.abort_active(writer)
                    prior_receipt = writer.snapshot.receipt
                    if prior_receipt is not None:
                        raise
                    failure: Exception = error
                    try:
                        await handle.control.close_scopes(writer.run_id, cancelled=True, suspended=False)
                    except Exception as cleanup_error:
                        failure = ExceptionGroup("Run execution and child cleanup failed", [error, cleanup_error])
                    await self.commit_failure(writer, handle.control, failure, publish)
                    if failure is not error:
                        raise failure from error
                    raise
            finally:
                self.observability.finish(run_span, writer.snapshot.state.value)
                self.observability.metrics.record_run(writer.snapshot.state.value)

    async def abort_active(self, writer: SessionWriter) -> None:
        """Retain interrupted observations while closing the active step."""
        active = await writer.active()
        if active is not None:
            await writer.abort(active.step_id)

    async def resolve_checkpoint(
        self,
        binding: RuntimeAgentBinding,
        writer: SessionWriter,
        checkpoint: RunCheckpoint,
        decisions: tuple[ResumeDecision, ...],
        policy: EffectivePolicy,
        publish: EventSink,
        control: RunControl,
        operations: OperationScope,
    ) -> ResumeProgress:
        """Validate pending tool bindings and resume only explicitly decided actions."""
        active = await writer.active()
        if active is None:
            step = await writer.begin(binding.agent.name)
            return ResumeProgress(step, checkpoint)
        if active.step_id != checkpoint.step_id:
            raise RuntimeStateError("The persisted step does not match its checkpoint.")
        if policy.deadline <= datetime.now(UTC):
            return ResumeProgress(active, checkpoint, LoopOutcome(LoopStatus.TIMED_OUT, usage=checkpoint.usage))
        assistant = next((value for value in active.messages if isinstance(value, AssistantMessage)), None)
        if assistant is None or not assistant.tool_calls:
            if not checkpoint.interruptions and not checkpoint.tool_results:
                return ResumeProgress(active, checkpoint)
            raise RuntimeStateError("Pending tool declarations are missing from the checkpoint.")
        tools = {tool.descriptor.code: tool for tool in binding.tools}
        current_usage = control.collect(writer.run_id, checkpoint.usage)
        results = {value.call_id: value for value in checkpoint.tool_results}
        decisions_by_id = {value.interruption_id: value for value in decisions}
        for interruption in checkpoint.interruptions:
            tool = tools.get(interruption.tool_code)
            call = next((value for value in assistant.tool_calls if value.id == interruption.call_id), None)
            if tool is None or call is None or call.name != interruption.tool_name:
                raise RuntimeStateError("The pending tool binding changed before resume.")
            if parse_tool_arguments(call.arguments) != interruption.arguments or not Draft202012Validator(
                tool.input_schema
            ).is_valid(interruption.arguments):
                raise RuntimeStateError("The pending tool argument contract changed before resume.")
            if tool.required_permissions - policy.grants.capabilities:
                raise RuntimeStateError("Current permissions do not grant the pending tool action.")
            decision = decisions_by_id.get(interruption.interruption_id)
            if decision is None:
                continue
            response = InterruptionResponse(interruption_id=decision.interruption_id, result=decision.result)
            context = ToolContext(
                call_id=call.id,
                run_id=writer.run_id,
                step_id=active.step_id,
                grants=policy.grants,
                parent_run_id=checkpoint.parent_run_id,
                parent_limits=ParentRunLimits(
                    deadline=policy.deadline,
                    remaining_model_steps=max(0, policy.max_model_steps - current_usage.total_model_calls),
                    remaining_output_tokens=max(0, policy.max_output_tokens - current_usage.budget_output_tokens),
                    tool_concurrency=policy.tool_concurrency,
                    child_model_calls=current_usage.child_model_calls,
                    child_budget_output_tokens=current_usage.child_budget_output_tokens,
                ),
            )
            outcome: ToolExecutionOutcome | None
            if interruption.phase == "authorization" and tool.policy.background:
                outcome = (
                    response.result
                    if not response.result.ok
                    else await operations.start(
                        tool,
                        ToolCall(
                            call_id=interruption.call_id,
                            tool_code=interruption.tool_code,
                            arguments=interruption.arguments,
                        ),
                        context,
                        authorization=ToolAuthorization(interruption, response),
                    )
                )
            else:
                outcome = await self.tool_runtime.resume_interruption(
                    tool, interruption, response, context, deadline=policy.deadline
                )
            if outcome is None:
                return ResumeProgress(active, checkpoint, LoopOutcome(LoopStatus.TIMED_OUT, usage=checkpoint.usage))
            current_usage = control.collect(writer.run_id, current_usage, outcome.usage_charges)
            if isinstance(outcome, ToolInterruption):
                replacement = tuple(
                    outcome if item.interruption_id == interruption.interruption_id else item
                    for item in checkpoint.interruptions
                    if item.call_id not in results
                )
                changed = checkpoint.model_copy(
                    update={
                        "interruptions": replacement,
                        "tool_results": tuple(results.values()),
                        "usage": current_usage,
                    }
                )
                return ResumeProgress(active, changed)
            results[call.id] = outcome
            current_usage = control.collect(writer.run_id, add_tool_usage(current_usage, 1))
            intents = await writer.store.tool_calls(writer.session_id, writer.run_id)
            intent = next((value for value in intents if value.call_id == call.id), None)
            if intent is None:
                raise RuntimeStateError("The persisted tool intent is missing before settlement.")
            if not (tool.policy.background and interruption.phase == "authorization" and decision.result.ok):
                await writer.save_tool_call(
                    intent.model_copy(update={"state": "succeeded" if outcome.ok else "failed", "result": outcome})
                )
            await publish(
                ToolCompletedEvent(
                    run_id=writer.run_id,
                    step_id=active.step_id,
                    assistant_message_id=assistant.message_id,
                    call_id=call.id,
                    tool_code=tool.descriptor.code,
                    result_message_id=tool_result_message(
                        call, outcome, run_id=writer.run_id, step_id=active.step_id
                    ).message_id,
                    ok=outcome.ok,
                )
            )
        pending = tuple(item for item in checkpoint.interruptions if item.call_id not in results)
        if pending:
            changed = checkpoint.model_copy(
                update={"interruptions": pending, "tool_results": tuple(results.values()), "usage": current_usage}
            )
            return ResumeProgress(active, changed)
        deferred = tuple(call for call in assistant.tool_calls if call.id not in results)
        if deferred and control.pause_requested:
            changed = checkpoint.model_copy(
                update={"interruptions": (), "tool_results": tuple(results.values()), "usage": current_usage}
            )
            return ResumeProgress(
                active, changed, LoopOutcome(LoopStatus.PAUSED, usage=current_usage, checkpoint=changed)
            )
        if deferred:
            outcomes = await self.loop.execute_tools(
                deferred,
                {tool.descriptor.name: tool for tool in (*binding.tools, ReadToolResultTool(writer))},
                writer=writer,
                operations=operations,
                step=current_usage.model_calls,
                policy=policy,
                control=control,
                memory_step_id=active.step_id,
                assistant_message_id=assistant.message_id,
                event_sink=publish,
                run_id=writer.run_id,
                parent_run_id=checkpoint.parent_run_id,
            )
            current_usage = control.usage
            completed = tuple(outcome for outcome in outcomes if isinstance(outcome, ToolResult))
            results.update((outcome.call_id, outcome) for outcome in completed)
            pending = tuple(outcome for outcome in outcomes if isinstance(outcome, ToolInterruption))
            if pending:
                changed = checkpoint.model_copy(
                    update={
                        "interruptions": pending,
                        "tool_results": tuple(results[call.id] for call in assistant.tool_calls if call.id in results),
                        "usage": current_usage,
                    }
                )
                return ResumeProgress(active, changed)
        messages = tuple(
            tool_result_message(call, results[call.id], run_id=writer.run_id, step_id=active.step_id)
            for call in assistant.tool_calls
        )
        await writer.complete(active.step_id, messages)
        for message in messages:
            await publish(
                MessageCompletedEvent(
                    run_id=writer.run_id, step_id=active.step_id, agent_name=binding.agent.name, message=message
                )
            )
        await publish(
            ModelStepCompletedEvent(run_id=writer.run_id, step_id=active.step_id, agent_name=binding.agent.name)
        )
        changed = checkpoint.model_copy(update={"interruptions": (), "tool_results": (), "usage": current_usage})
        return ResumeProgress(
            await writer.begin(binding.agent.name, consume_inputs=not control.pause_requested), changed
        )

    async def cancel_paused(self, *, session: Session, checkpoint: RunCheckpoint, reason: str) -> RunResult:
        """Settle a paused invocation by a single checkpoint comparison update."""
        if not reason.strip():
            raise ValueError("Cancellation requires a reason")
        snapshot = await session.store.cancel_paused(session.session_id, checkpoint=checkpoint, reason=reason)
        if snapshot.receipt is None:
            raise RuntimeStateError("Cancelling a paused Run did not commit a receipt.")
        return RunResult(snapshot.receipt)

    async def reconcile(
        self,
        *,
        session: Session,
        run_id: str,
        request_id: str,
        expected_epoch: int,
        expected_revision: int,
        evidence: tuple[ActionEvidence, ...] = (),
    ) -> ReconcileResult:
        """Fence a lost owner and reconcile persisted facts with trusted host evidence.

        The host must hold exclusive deployment ownership before calling this
        local operation. External adapter queries happen before this boundary.
        """
        payload = {
            "run_id": run_id,
            "expected_epoch": expected_epoch,
            "expected_revision": expected_revision,
            "evidence": [value.model_dump(mode="json") for value in evidence],
        }
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return await session.store.reconcile(
            session.session_id,
            run_id=run_id,
            request_id=request_id,
            parameters_digest=digest,
            expected_epoch=expected_epoch,
            expected_revision=expected_revision,
            evidence=evidence,
        )

    async def aclose(self) -> None:
        """Cancel all still-active locally owned scopes and await cleanup."""
        async with asyncio.TaskGroup() as group:
            for handle in tuple(self.handles.values()):
                group.create_task(handle.aclose())

    def resolve(self, agent: Agent) -> tuple[tuple[BaseTool, ...], tuple[Skill, ...]]:
        """Resolve capability bindings before a Run creates a new ModelStep."""
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
        if any(tool.policy.background and not tool.supports_background for tool in tools):
            raise AgentConfigurationError("Background Tools require an explicit backend acceptance implementation.")
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
                RunLimits(
                    execution_timeout_seconds=policy.timeout_seconds,
                    max_model_steps=policy.max_model_steps,
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
                "max_model_steps": agent.policy_hints.max_model_steps,
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


def validate_resolutions(
    interruptions: tuple[ToolInterruption, ...],
    resolutions: tuple[ResumeDecision, ...],
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
