"""The minimal provider-neutral ReAct loop."""

import asyncio
import json
from collections.abc import Mapping
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from uuid import uuid4

from jsonschema import Draft202012Validator

from pleiad.agent.contracts import AgentContext, AgentDefinition, AgentResult, AgentStatus
from pleiad.agent.prompt import PromptBuilder
from pleiad.context.budget import BudgetGuard
from pleiad.context.compaction import ContextCompactor
from pleiad.context.request import RequestAssembler
from pleiad.errors import ErrorCode, ErrorInfo, PleiadError, make_error
from pleiad.llm.client import LLMStreamEventKind
from pleiad.llm.contracts import (
    LLMRequest,
    Message,
    MessageRole,
)
from pleiad.llm.contracts import (
    ToolCall as LLMToolCall,
)
from pleiad.llm.response.envelope import LLMResponse
from pleiad.memory.store import ModelMessageSink
from pleiad.observability import (
    ObservabilityPort,
    SpanKind,
    SpanScope,
    observe_span,
    observed,
)
from pleiad.runtime.capabilities import WorkspaceCapabilitySync
from pleiad.runtime.events import EventBus
from pleiad.skills.contracts import Skill
from pleiad.skills.zoo import SkillZoo
from pleiad.tools.base import BaseTool
from pleiad.tools.contracts import ToolCall, ToolContext, ToolError, ToolResult
from pleiad.tools.runtime import ToolRuntime
from pleiad.tools.zoo import ToolZoo


class AgentLoop:
    """Run Model -> Tool -> Observation until a final result or terminal error."""

    def __init__(
        self,
        tool_runtime: ToolRuntime,
        events: EventBus | None = None,
        observability: ObservabilityPort | None = None,
        tools: ToolZoo | None = None,
        skills: SkillZoo | None = None,
        capability_sync: WorkspaceCapabilitySync | None = None,
        budget: BudgetGuard | None = None,
    ) -> None:
        self.tool_runtime = tool_runtime
        self.events = events
        self.observability = observability
        self.tools = tools
        self.skills = skills
        self.capability_sync = capability_sync
        self.prompts = PromptBuilder()
        self.request_assembler = RequestAssembler()
        self.budget = budget or BudgetGuard()
        self.compactor = ContextCompactor(self.budget)

    @observed(
        name="agent.run",
        kind=SpanKind.AGENT,
        attributes=lambda args, kwargs: {
            "agent_id": getattr(args[1], "agent_id", "unknown"),
            "model_role": getattr(args[1], "model_role", "unknown"),
        },
    )
    async def run(
        self,
        definition: AgentDefinition,
        user_message: str,
        *,
        execution_id: str,
        node_run_id: str | None = None,
        context: AgentContext | None = None,
        message_sink: ModelMessageSink | None = None,
    ) -> AgentResult:
        """Run one Agent with an automatically managed Agent Span."""
        return await self._run(
            definition,
            user_message,
            execution_id=execution_id,
            node_run_id=node_run_id,
            context=context,
            message_sink=message_sink,
        )

    async def _run(
        self,
        definition: AgentDefinition,
        user_message: str,
        *,
        execution_id: str,
        node_run_id: str | None = None,
        context: AgentContext | None = None,
        message_sink: ModelMessageSink | None = None,
    ) -> AgentResult:
        """Continue the ReAct loop until the Model finishes or an error is terminal."""
        run_id = uuid4().hex
        messages = list(context.messages if context else ())
        if not messages or messages[0].role is not MessageRole.SYSTEM:
            messages.insert(0, Message(role=MessageRole.SYSTEM, content=definition.prompt))
        messages.append(Message(role=MessageRole.USER, content=user_message))
        tool_results: list[ToolResult] = []
        latest_response: LLMResponse | None = None
        compaction_attempts = 0
        conversation_id = context.conversation_id if context else "default-conversation"
        history_sequence = context.history_sequence if context else 0
        checkpoint_sink = context.checkpoint_sink if context else None
        self._emit(
            execution_id,
            "agent.started",
            {"run_id": run_id, "agent_id": definition.agent_id},
        )

        async def run_iteration(iteration: int) -> AgentResult | None:
            nonlocal compaction_attempts, latest_response
            try:
                if self.capability_sync:
                    self.capability_sync.sync(execution_id=execution_id)
                active_tools = self._active_tools(definition)
                tools_by_name = {tool.descriptor.name: tool for tool in active_tools}
                tool_definitions = tuple(tool.model_definition for tool in active_tools)
                messages[0] = Message(
                    role=MessageRole.SYSTEM,
                    content=self._prompt(definition),
                )
                request = self.request_assembler.assemble(
                    definition,
                    tuple(messages),
                    tool_definitions,
                    system_prompt=messages[0].content or "",
                )
                budget_report = self.budget.check(request)
                self._emit(
                    execution_id,
                    "context.budget.checked",
                    {
                        "run_id": run_id,
                        "iteration": iteration,
                        "estimated_tokens": budget_report.estimated_tokens,
                        "usable_input_tokens": budget_report.usable_input_tokens,
                        "compaction_required": budget_report.requires_compaction,
                    },
                )
                if budget_report.requires_compaction and compaction_attempts == 0:
                    compaction_attempts += 1
                    compacted = self.compactor.compact(
                        request,
                        conversation_id=conversation_id,
                        execution_id=execution_id,
                        covered_until_sequence=max(
                            0,
                            history_sequence - self.compactor.retained_message_count + 1,
                        ),
                        raw_query=context.raw_query if context else None,
                        prior_handoff=context.checkpoint_handoff if context else None,
                    )
                    messages[:] = compacted.messages
                    if compacted.checkpoint is not None:
                        self._emit(
                            execution_id,
                            "context.compacted",
                            {
                                "run_id": run_id,
                                "input_tokens_before": (compacted.checkpoint.input_tokens_before),
                                "input_tokens_after": compacted.checkpoint.input_tokens_after,
                            },
                        )
                        if checkpoint_sink is not None:
                            checkpoint_sink(compacted.checkpoint)
                    request = self.request_assembler.assemble(
                        definition,
                        tuple(messages),
                        tool_definitions,
                        system_prompt=messages[0].content or "",
                    )
                    budget_report = self.budget.check(request)
                if budget_report.estimated_tokens > budget_report.usable_input_tokens:
                    return AgentResult(
                        run_id=run_id,
                        status=AgentStatus.FAILED,
                        response=latest_response,
                        tool_results=tuple(tool_results),
                        error=make_error(
                            ErrorCode.AGENT_MODEL,
                            details={
                                "reason": "context_budget_exceeded",
                                "estimated_tokens": budget_report.estimated_tokens,
                                "usable_input_tokens": budget_report.usable_input_tokens,
                            },
                        ),
                    )
                model_span_id: str | None = None
                with self._span(
                    "model.stream",
                    kind=SpanKind.MODEL,
                    attributes={
                        "model": definition.client.model,
                        "request_id": request.request_id,
                    },
                ) as model_span:
                    latest_response = await self._stream_response(
                        definition,
                        request,
                        execution_id=execution_id,
                        run_id=run_id,
                    )
                    if isinstance(model_span, SpanScope):
                        model_span_id = model_span.span_id
                        model_span.set_attributes(_model_attributes(latest_response))
                        model_span.set_attribute("iteration", iteration)
            except PleiadError as exc:
                return AgentResult(
                    run_id=run_id,
                    status=AgentStatus.FAILED,
                    response=latest_response,
                    tool_results=tuple(tool_results),
                    error=exc.info,
                )
            except Exception as exc:
                return AgentResult(
                    run_id=run_id,
                    status=AgentStatus.FAILED,
                    response=latest_response,
                    tool_results=tuple(tool_results),
                    error=make_error(
                        ErrorCode.AGENT_MODEL,
                        details={"exception": exc.__class__.__name__},
                    ),
                )

            response = latest_response
            response_message = Message(
                role=MessageRole.ASSISTANT,
                content=response.text or None,
                thinking=response.thinking or None,
                tool_calls=response.tool_calls,
            )
            messages.append(response_message)
            if message_sink is not None:
                message_sink(run_id, response_message)
            self._emit(
                execution_id,
                "agent.response",
                {"run_id": run_id, "has_tool_calls": response.has_tool_calls},
            )
            if not response.has_tool_calls:
                return self._final_result(
                    run_id,
                    definition,
                    response,
                    tuple(tool_results),
                )

            async def execute_tool_call(
                model_call: LLMToolCall,
                available_tools: dict[str, BaseTool] = tools_by_name,
            ) -> ToolResult:
                tool = available_tools.get(model_call.name)
                tool_code = tool.descriptor.code if tool else f"unknown:{model_call.name}"
                self._emit(
                    execution_id,
                    "tool.started",
                    {
                        "run_id": run_id,
                        "tool_code": tool_code,
                        "tool_name": model_call.name,
                        "call_id": model_call.call_id,
                    },
                )
                if tool is None:
                    info = make_error(
                        ErrorCode.TOOL_UNKNOWN,
                        details={"tool_name": model_call.name},
                    )
                    result = self._tool_failure(model_call.call_id, info)
                elif model_call.arguments is None and model_call.raw_arguments is not None:
                    info = make_error(
                        ErrorCode.TOOL_INVALID_ARGUMENTS,
                        details=_invalid_tool_arguments_details(model_call.raw_arguments),
                    )
                    result = self._tool_failure(model_call.call_id, info)
                else:
                    call = ToolCall(
                        call_id=model_call.call_id,
                        tool_code=tool.descriptor.code,
                        arguments=model_call.arguments or {},
                    )
                    result = await self.tool_runtime.execute(
                        tool,
                        call,
                        ToolContext(
                            execution_id=execution_id,
                            agent_run_id=run_id,
                            node_run_id=node_run_id,
                            permissions=definition.permissions,
                        ),
                        caused_by_span_id=model_span_id,
                    )
                elapsed_ms = max(
                    0,
                    int((datetime.now(UTC) - result.started_at).total_seconds() * 1000),
                )
                self._emit(
                    execution_id,
                    "tool.succeeded" if result.ok else "tool.failed",
                    {
                        "run_id": run_id,
                        "tool_code": tool_code,
                        "tool_name": model_call.name,
                        "call_id": model_call.call_id,
                        "ok": result.ok,
                        "elapsed_ms": elapsed_ms,
                        "error": result.error.model_dump(mode="json") if result.error else None,
                    },
                )
                return result

            call_results = await asyncio.gather(
                *(execute_tool_call(model_call) for model_call in response.tool_calls)
            )
            for model_call, result in zip(response.tool_calls, call_results, strict=True):
                tool_results.append(result)
                tool_message = Message(
                    role=MessageRole.TOOL,
                    tool_call_id=model_call.call_id,
                    name=model_call.name,
                    content=json.dumps(
                        {
                            "ok": result.ok,
                            "output": result.output,
                            "error": (
                                result.error.model_dump(mode="json") if result.error else None
                            ),
                        },
                        ensure_ascii=False,
                    ),
                )
                messages.append(tool_message)
                if message_sink is not None:
                    message_sink(run_id, tool_message)
                if result.error and result.error.terminal:
                    return AgentResult(
                        run_id=run_id,
                        status=AgentStatus.FAILED,
                        response=response,
                        tool_results=tuple(tool_results),
                        error=self._error_info(result.error),
                    )
            return None

        async def loop() -> AgentResult:
            iteration = 0
            while True:
                iteration += 1
                with self._span(
                    "agent.loop",
                    kind=SpanKind.LOOP,
                    attributes={"iteration": iteration},
                ):
                    result = await run_iteration(iteration)
                if result is not None:
                    return result

        try:
            result = await asyncio.wait_for(loop(), timeout=definition.policy.timeout_seconds)
        except TimeoutError:
            result = AgentResult(
                run_id=run_id,
                status=AgentStatus.FAILED,
                response=latest_response,
                tool_results=tuple(tool_results),
                error=make_error(ErrorCode.AGENT_MODEL, details={"reason": "timeout"}),
            )
        except asyncio.CancelledError:
            result = AgentResult(
                run_id=run_id,
                status=AgentStatus.CANCELLED,
                response=latest_response,
                tool_results=tuple(tool_results),
                error=make_error(ErrorCode.AGENT_CANCELLED),
            )
        except PleiadError as exc:
            result = AgentResult(
                run_id=run_id,
                status=AgentStatus.FAILED,
                response=latest_response,
                tool_results=tuple(tool_results),
                error=exc.info,
            )
        except Exception as exc:
            result = AgentResult(
                run_id=run_id,
                status=AgentStatus.FAILED,
                response=latest_response,
                tool_results=tuple(tool_results),
                error=make_error(
                    ErrorCode.SYSTEM_UNEXPECTED,
                    details={"exception": exc.__class__.__name__},
                ),
            )
        self._emit(
            execution_id,
            "agent.finished",
            {
                "run_id": run_id,
                "status": result.status.value,
                "error": result.error.as_dict() if result.error else None,
            },
        )
        return result

    async def _stream_response(
        self,
        definition: AgentDefinition,
        request: LLMRequest,
        *,
        execution_id: str,
        run_id: str,
    ) -> LLMResponse:
        """Consume one provider stream while publishing visible text deltas."""
        response: LLMResponse | None = None
        async for event in definition.client.stream(request):
            if event.kind is LLMStreamEventKind.TEXT_DELTA and event.text:
                self._emit(
                    execution_id,
                    "agent.text.delta",
                    {"run_id": run_id, "agent_id": definition.agent_id, "delta": event.text},
                )
            elif event.kind is LLMStreamEventKind.THINKING_DELTA and event.text:
                self._emit(
                    execution_id,
                    "agent.thinking.delta",
                    {"run_id": run_id, "agent_id": definition.agent_id, "delta": event.text},
                )
            elif event.kind is LLMStreamEventKind.COMPLETED:
                response = event.response
        if response is None:
            raise RuntimeError("LLM stream ended without a completed response")
        return response

    def _active_tools(self, definition: AgentDefinition) -> tuple[BaseTool, ...]:
        """Keep built-ins resident and reload selected external Tools per request."""
        active: dict[str, BaseTool] = {tool.descriptor.name: tool for tool in definition.tools}
        if self.tools is not None:
            for code in definition.dynamic_tool_codes:
                tool = self.tools.load(code)
                active.setdefault(tool.descriptor.name, tool)
        return tuple(active.values())

    def _prompt(self, definition: AgentDefinition) -> str:
        """Reload selected Skill bodies immediately before each model request."""
        skills: tuple[Skill, ...] = ()
        if self.skills is not None:
            skills = tuple(self.skills.load(code) for code in definition.dynamic_skill_codes)
        return self.prompts.build(definition.prompt, skills)

    @staticmethod
    def _final_result(
        run_id: str,
        definition: AgentDefinition,
        response: LLMResponse,
        tool_results: tuple[ToolResult, ...],
    ) -> AgentResult:
        """Validate the only normal termination: the Model's final response."""
        if definition.output_schema is None:
            return AgentResult(
                run_id=run_id,
                status=AgentStatus.SUCCEEDED,
                text=response.text,
                structured_output=response.structured_output,
                response=response,
                tool_results=tool_results,
            )
        try:
            structured = response.structured_output
            if structured is None:
                structured = json.loads(response.text)
            errors = sorted(
                Draft202012Validator(definition.output_schema).iter_errors(structured),
                key=lambda error: error.path,
            )
            if errors:
                return AgentResult(
                    run_id=run_id,
                    status=AgentStatus.FAILED,
                    text=response.text,
                    response=response,
                    tool_results=tool_results,
                    error=make_error(
                        ErrorCode.AGENT_OUTPUT,
                        details={"reason": errors[0].message},
                    ),
                )
            return AgentResult(
                run_id=run_id,
                status=AgentStatus.SUCCEEDED,
                text=response.text,
                structured_output=structured,
                response=response,
                tool_results=tool_results,
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            return AgentResult(
                run_id=run_id,
                status=AgentStatus.FAILED,
                text=response.text,
                response=response,
                tool_results=tool_results,
                error=make_error(
                    ErrorCode.AGENT_OUTPUT,
                    details={"exception": exc.__class__.__name__},
                ),
            )

    @staticmethod
    def _tool_failure(call_id: str, info: ErrorInfo) -> ToolResult:
        """Turn a catalogued error into a Tool observation."""
        now = datetime.now(UTC)
        return ToolResult(
            call_id=call_id,
            ok=False,
            error=ToolError(
                code=info.code,
                message=info.message,
                kind=info.kind,
                retryable=info.retryable,
                terminal=info.terminal,
                details=info.details,
            ),
            started_at=now,
            finished_at=now,
        )

    @staticmethod
    def _error_info(error: ToolError) -> ErrorInfo:
        """Project a terminal Tool error into the Agent result contract."""
        return ErrorInfo(
            code=error.code,
            message=error.message,
            kind=error.kind,
            retryable=error.retryable,
            terminal=error.terminal,
            details=error.details,
        )

    def _emit(self, execution_id: str, event_type: str, data: Mapping[str, object]) -> None:
        if self.events:
            self.events.emit(execution_id, event_type, data)

    def _span(
        self,
        name: str,
        *,
        kind: SpanKind,
        attributes: Mapping[str, object],
    ) -> AbstractContextManager[SpanScope | None]:
        """Return a no-op context when tracing is not active."""
        return observe_span(
            self.observability,
            name,
            kind=kind,
            attributes=attributes,
        )


def _model_attributes(response: LLMResponse) -> dict[str, object]:
    """Project provider response facts without retaining prompt or response bodies."""
    return {
        "protocol": response.protocol.value,
        "provider": response.provider.value,
        "model": response.model,
        "response_id": response.response_id,
        "finish_reason": response.finish_reason.value,
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
        "total_tokens": response.usage.total_tokens,
        "reasoning_tokens": response.usage.reasoning_tokens,
        "cached_input_tokens": response.usage.cached_input_tokens,
    }


def _invalid_tool_arguments_details(raw_arguments: str) -> dict[str, object]:
    """Describe malformed Model Tool arguments without retaining their contents."""
    try:
        parsed = json.loads(raw_arguments)
    except json.JSONDecodeError as exc:
        return {
            "reason": f"工具参数不是有效的 JSON: {exc.msg}",
            "line": exc.lineno,
            "column": exc.colno,
            "position": exc.pos,
        }
    return {
        "reason": "工具参数必须是 JSON 对象",
        "received_type": type(parsed).__name__,
    }
