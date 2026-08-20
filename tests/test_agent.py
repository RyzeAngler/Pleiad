"""ReAct loop and graph executor integration tests with a fake Model."""

import asyncio
import json
from pathlib import Path
from typing import cast

from conftest import FakeLLMClient

from pleiad.agent.contracts import AgentContext, AgentDefinition, AgentStatus
from pleiad.agent.loop import AgentLoop
from pleiad.llm.capabilities import LLMCapabilities
from pleiad.llm.contracts import ResponseFormatType
from pleiad.llm.response.parts import TextPart, ToolCallPart
from pleiad.memory import (
    JsonlMainAgentMemory,
    LongTermMemory,
    LongTermMemoryKind,
    MemoryScope,
)
from pleiad.observability import SpanKind, TraceRecordType, TraceService, create_tracer
from pleiad.observability.stores.jsonl import JsonlTraceStore
from pleiad.runtime.events import EventBus
from pleiad.skills.contracts import Skill, SkillDescriptor
from pleiad.skills.zoo import SkillZoo
from pleiad.tools.base import FunctionTool
from pleiad.tools.contracts import ToolContext, ToolDescriptor
from pleiad.tools.runtime import ToolRuntime
from pleiad.tools.zoo import ToolZoo


def test_react_loop_executes_tool_then_returns_structured_output() -> None:
    async def execute(context: ToolContext, arguments: dict[str, object]) -> dict[str, object]:
        return {"value": cast(int, arguments["value"]) + 1}

    tool = FunctionTool(
        ToolDescriptor(name="increment", code="test.increment", description="加一"),
        execute,
        {
            "type": "object",
            "properties": {"value": {"type": "integer"}},
            "required": ["value"],
            "additionalProperties": False,
        },
    )
    client = FakeLLMClient(
        [
            (ToolCallPart(call_id="call-1", name="increment", arguments={"value": 2}),),
            (TextPart(text='{"value": 3}'),),
        ]
    )
    events = EventBus()
    result = asyncio.run(
        AgentLoop(ToolRuntime(), events).run(
            AgentDefinition(
                agent_id="test-agent",
                model_role="text",
                client=client,
                prompt="完成任务",
                tools=(tool,),
                output_schema={"type": "object", "required": ["value"]},
            ),
            "输入",
            execution_id="execution-1",
        )
    )
    assert result.status is AgentStatus.SUCCEEDED
    assert result.structured_output == {"value": 3}
    assert len(result.tool_results) == 1
    assert client.requests[0].response_format.type is ResponseFormatType.JSON_OBJECT
    assert [event.event_type for event in events.list("execution-1")] == [
        "agent.started",
        "context.budget.checked",
        "agent.response",
        "tool.started",
        "tool.succeeded",
        "context.budget.checked",
        "agent.text.delta",
        "agent.response",
        "agent.finished",
    ]


def test_main_agent_memory_persists_raw_history_without_automatic_recall(
    tmp_path: Path,
) -> None:
    """Persist raw history while keeping long-term retrieval explicitly model-led."""
    memory = JsonlMainAgentMemory(tmp_path / "memory")
    first_turn = memory.begin_turn(
        "conversation-1",
        "execution-1",
        "remember the first answer",
    )
    first_client = FakeLLMClient([(TextPart(text="the first answer"),)])
    first_result = asyncio.run(
        AgentLoop(ToolRuntime()).run(
            AgentDefinition(
                agent_id="pleiad-supervisor",
                model_role="text",
                client=first_client,
                prompt="system prompt",
            ),
            '{"objective":"remember the first answer","assembled":"must not persist"}',
            execution_id="execution-1",
            context=AgentContext(messages=first_turn.history),
            message_sink=first_turn.append_model_message,
        )
    )
    memory.finish_turn(first_turn, first_result.text, succeeded=True)

    reloaded = JsonlMainAgentMemory(tmp_path / "memory")
    second_turn = reloaded.begin_turn(
        "conversation-1",
        "execution-2",
        "continue the first answer",
    )

    assert [message.content for message in second_turn.history] == [
        "remember the first answer",
        "the first answer",
    ]
    persisted = reloaded.list_model_messages("conversation-1")
    assert all("must not persist" not in (item.message.content or "") for item in persisted)
    assert reloaded.list_long_term("conversation-1") == ()
    assert len(reloaded.list_conversation("conversation-1")) == 3

    reloaded.append_long_term(
        LongTermMemory(
            memory_id="first-answer",
            scope=MemoryScope.CONVERSATION,
            scope_id="conversation-1",
            conversation_id="conversation-1",
            kind=LongTermMemoryKind.SUMMARY,
            content="The first answer was persisted as visible conversation context.",
        )
    )
    recalled = reloaded.recall_memory(
        conversation_id="conversation-1",
        query="what was persisted",
        limit=5,
        policy=reloaded.policy,
    )
    assert recalled[0].memory_id == "first-answer"


def test_agent_passes_output_schema_to_models_with_schema_support() -> None:
    client = FakeLLMClient([(TextPart(text='{"value": 3}'),)])
    client.capabilities = LLMCapabilities(
        response_formats=frozenset({ResponseFormatType.TEXT, ResponseFormatType.JSON_SCHEMA}),
        strict_json_schema=True,
    )
    output_schema = {
        "type": "object",
        "properties": {"value": {"type": "integer"}},
        "required": ["value"],
        "additionalProperties": False,
    }

    result = asyncio.run(
        AgentLoop(ToolRuntime()).run(
            AgentDefinition(
                agent_id="schema-agent",
                model_role="text",
                client=client,
                prompt="完成任务",
                output_schema=output_schema,
            ),
            "输入",
            execution_id="execution-schema",
        )
    )

    response_format = client.requests[0].response_format
    assert result.status is AgentStatus.SUCCEEDED
    assert response_format.type is ResponseFormatType.JSON_SCHEMA
    assert response_format.json_schema == output_schema
    assert response_format.strict is True


def test_agent_streams_visible_text_before_the_final_response() -> None:
    """Visible Model text is published as Runtime Events while it arrives."""
    client = FakeLLMClient([(TextPart(text="逐字回答"),)])
    events = EventBus()

    result = asyncio.run(
        AgentLoop(ToolRuntime(), events).run(
            AgentDefinition(
                agent_id="stream-agent",
                model_role="text",
                client=client,
                prompt="完成任务",
            ),
            "输入",
            execution_id="execution-stream",
        )
    )

    deltas = [
        event.data["delta"]
        for event in events.list("execution-stream")
        if event.event_type == "agent.text.delta"
    ]
    assert result.text == "逐字回答"
    assert deltas == ["逐字回答"]


def test_dynamic_tools_and_skills_are_loaded_before_each_model_request() -> None:
    """Selected external capabilities are refreshed for every ReAct turn."""

    async def execute(context: ToolContext, arguments: dict[str, object]) -> dict[str, object]:
        return {"value": arguments["value"]}

    tools = ToolZoo()
    tools.register(
        ToolDescriptor(name="echo", code="test.echo", description="回显"),
        lambda descriptor: FunctionTool(
            descriptor,
            execute,
            {
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
                "additionalProperties": False,
            },
        ),
    )
    skills = SkillZoo()
    versions = 0

    def load_skill(descriptor: SkillDescriptor) -> Skill:
        nonlocal versions
        versions += 1
        return Skill(descriptor=descriptor, instructions=f"版本 {versions}")

    skills.register(
        SkillDescriptor(name="dynamic", code="test.dynamic", description="动态知识"),
        load_skill,
    )
    client = FakeLLMClient(
        [
            (ToolCallPart(call_id="call-1", name="echo", arguments={"value": "ok"}),),
            (TextPart(text="完成"),),
        ]
    )

    result = asyncio.run(
        AgentLoop(ToolRuntime(), tools=tools, skills=skills).run(
            AgentDefinition(
                agent_id="dynamic-agent",
                model_role="text",
                client=client,
                prompt="完成任务",
                dynamic_tool_codes=("test.echo",),
                dynamic_skill_codes=("test.dynamic",),
            ),
            "输入",
            execution_id="execution-dynamic",
        )
    )

    assert result.status is AgentStatus.SUCCEEDED
    assert len(client.requests) == 2
    first_prompt = client.requests[0].messages[0].content
    second_prompt = client.requests[1].messages[0].content
    assert first_prompt is not None and first_prompt.endswith("版本 1")
    assert second_prompt is not None and second_prompt.endswith("版本 2")
    assert [tool.name for tool in client.requests[0].tools] == ["echo"]
    assert [tool.name for tool in client.requests[1].tools] == ["echo"]


def test_react_loop_records_agent_and_model_spans(tmp_path: Path) -> None:
    """Agent and Model boundaries become nested Trace spans."""
    client = FakeLLMClient([(TextPart(text="完成"),)])
    trace_service = TraceService(JsonlTraceStore(tmp_path / "traces"))
    tracer = create_tracer(trace_service)

    with tracer.trace("execution-span"):
        result = asyncio.run(
            AgentLoop(ToolRuntime(observability=tracer), observability=tracer).run(
                AgentDefinition(
                    agent_id="span-agent",
                    model_role="text",
                    client=client,
                    prompt="完成任务",
                ),
                "输入",
                execution_id="execution-span",
            )
        )

    records = trace_service.list("execution-span")
    trace_service.close()

    assert result.status is AgentStatus.SUCCEEDED
    assert [record.name for record in records if record.span_id is not None] == [
        "agent.run",
        "agent.loop",
        "model.stream",
        "model.stream",
        "agent.loop",
        "agent.run",
    ]


def test_react_trace_records_loop_usage_and_tool_causality(tmp_path: Path) -> None:
    """The Trace tree keeps loop context, model usage, and Model->Tool causality."""

    async def execute(context: ToolContext, arguments: dict[str, object]) -> dict[str, object]:
        return {"value": arguments["value"]}

    tool = FunctionTool(
        ToolDescriptor(name="echo", code="test.echo", description="回显"),
        execute,
        {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        },
    )
    client = FakeLLMClient(
        [
            (ToolCallPart(call_id="call-1", name="echo", arguments={"value": "ok"}),),
            (TextPart(text="完成"),),
        ]
    )
    trace_service = TraceService(JsonlTraceStore(tmp_path / "traces"))
    tracer = create_tracer(trace_service)

    with tracer.trace("execution-causality"):
        result = asyncio.run(
            AgentLoop(ToolRuntime(observability=tracer), observability=tracer).run(
                AgentDefinition(
                    agent_id="causality-agent",
                    model_role="text",
                    client=client,
                    prompt="完成任务",
                    tools=(tool,),
                ),
                "输入",
                execution_id="execution-causality",
            )
        )

    records = trace_service.list("execution-causality")
    trace_service.close()

    assert result.status is AgentStatus.SUCCEEDED
    loop_started = next(
        record
        for record in records
        if record.record_type is TraceRecordType.SPAN_STARTED and record.kind is SpanKind.LOOP
    )
    assert loop_started.attributes["iteration"] == 1
    model_ended = next(
        record
        for record in records
        if record.record_type is TraceRecordType.SPAN_ENDED and record.kind is SpanKind.MODEL
    )
    assert model_ended.attributes["provider"] == "openai"
    assert model_ended.attributes["total_tokens"] == 0
    link = next(record for record in records if record.record_type is TraceRecordType.LINK_CREATED)
    tool_started = next(
        record
        for record in records
        if record.record_type is TraceRecordType.SPAN_STARTED and record.kind is SpanKind.TOOL
    )
    assert link.source_span_id == model_ended.span_id
    assert link.target_span_id == tool_started.span_id
    assert link.relation == "caused_by"
    assert link.correlation_id == "call-1"


def test_recoverable_tool_error_becomes_observation_and_loop_continues() -> None:
    """A parameter error is an Observation, not an artificial loop stop."""

    async def execute(context: ToolContext, arguments: dict[str, object]) -> dict[str, object]:
        return {"value": arguments["value"]}

    tool = FunctionTool(
        ToolDescriptor(name="echo", code="test.echo", description="回显"),
        execute,
        {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        },
    )
    client = FakeLLMClient(
        [
            (ToolCallPart(call_id="call-1", name="echo", arguments={}),),
            (TextPart(text='{"value": "recovered"}'),),
        ]
    )
    result = asyncio.run(
        AgentLoop(ToolRuntime()).run(
            AgentDefinition(
                agent_id="recoverable-agent",
                model_role="text",
                client=client,
                prompt="修正参数",
                tools=(tool,),
                output_schema={"type": "object", "required": ["value"]},
            ),
            "输入",
            execution_id="execution-2",
        )
    )
    assert result.status is AgentStatus.SUCCEEDED
    assert result.tool_results[0].error
    assert result.tool_results[0].error.code == "tool.invalid_arguments"


def test_invalid_raw_tool_arguments_report_json_location_and_loop_continues() -> None:
    """Malformed Model JSON remains a recoverable and actionable Tool observation."""

    async def execute(context: ToolContext, arguments: dict[str, object]) -> dict[str, object]:
        return {"value": arguments["value"]}

    tool = FunctionTool(
        ToolDescriptor(name="echo", code="test.echo", description="回显"),
        execute,
        {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        },
    )
    client = FakeLLMClient(
        [
            (
                ToolCallPart(
                    call_id="call-invalid-json",
                    name="echo",
                    raw_arguments='{"value":',
                ),
            ),
            (TextPart(text="已修正"),),
        ]
    )
    events = EventBus()

    result = asyncio.run(
        AgentLoop(ToolRuntime(), events).run(
            AgentDefinition(
                agent_id="invalid-json-agent",
                model_role="text",
                client=client,
                prompt="修正参数",
                tools=(tool,),
            ),
            "输入",
            execution_id="execution-invalid-json",
        )
    )

    error = result.tool_results[0].error
    assert result.status is AgentStatus.SUCCEEDED
    assert error is not None
    assert error.code == "tool.invalid_arguments"
    assert error.details["reason"] == "工具参数不是有效的 JSON: Expecting value"
    assert error.details["line"] == 1
    assert error.details["column"] == 10
    observation = json.loads(cast(str, client.requests[1].messages[-1].content))
    assert observation["error"]["details"]["column"] == 10
    assert [event.event_type for event in events.list("execution-invalid-json")] == [
        "agent.started",
        "context.budget.checked",
        "agent.response",
        "tool.started",
        "tool.failed",
        "context.budget.checked",
        "agent.text.delta",
        "agent.response",
        "agent.finished",
    ]
