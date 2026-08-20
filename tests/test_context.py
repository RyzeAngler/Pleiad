"""Context construction, Memory scope, and compaction contract tests."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from pleiad.context import (
    BudgetGuard,
    ContextBudgetPolicy,
    ContextBuilder,
    ContextCompactor,
    ContextRenderer,
    ContextRequest,
)
from pleiad.llm.contracts import LLMRequest, Message, MessageRole
from pleiad.memory import (
    JsonlMainAgentMemory,
    LongTermMemory,
    LongTermMemoryKind,
    MemoryCandidate,
    MemoryPolicy,
    MemoryScope,
)
from pleiad.memory.tool import memory_recall_tool
from pleiad.tools.contracts import ToolContext


def test_context_renderer_keeps_profile_and_checkpoint_as_reference_blocks() -> None:
    candidate = MemoryCandidate(
        memory_id="profile-language",
        scope=MemoryScope.USER,
        content="The preferred response language is English.",
        kind=LongTermMemoryKind.FACT,
        source_message_ids=("message-1",),
        score=None,
        updated_at=datetime.now(UTC),
        metadata={},
    )
    request = ContextRequest(
        agent_id="pleiad-supervisor",
        conversation_id="conversation-1",
        execution_id="execution-1",
        raw_query="prepare a report",
        business_input={"format": "markdown"},
        personal_profile=(candidate,),
        checkpoint_handoff="Continue from the validated artifact.",
    )

    bundle = ContextBuilder().build(request)
    rendered = ContextRenderer().render(request, bundle)

    assert [block.source_type for block in bundle.blocks] == [
        "context.checkpoint",
        "memory.profile",
    ]
    assert '"source_type": "memory.profile"' in rendered
    assert '"reference_policy"' in rendered
    assert "Continue from the validated artifact." in rendered


def test_jsonl_memory_loads_l2_only_when_cross_conversation_is_enabled(tmp_path: Path) -> None:
    disabled = JsonlMainAgentMemory(tmp_path / "disabled")
    profile = LongTermMemory(
        memory_id="profile-language",
        scope=MemoryScope.USER,
        scope_id="default-user",
        kind=LongTermMemoryKind.FACT,
        content="The preferred response language is English.",
    )
    with pytest.raises(ValueError, match="disabled"):
        disabled.append_long_term(profile)

    enabled = JsonlMainAgentMemory(
        tmp_path / "enabled",
        MemoryPolicy(cross_conversation_memory=True),
    )
    enabled.append_long_term(profile)
    turn = enabled.begin_turn("conversation-1", "execution-1", "continue")

    assert [item.memory_id for item in turn.personal_profile] == ["profile-language"]


def test_memory_recall_tool_bounds_the_model_visible_result(tmp_path: Path) -> None:
    policy = MemoryPolicy(
        recall_max_output_tokens=1,
        recall_max_results=1,
    )
    memory = JsonlMainAgentMemory(tmp_path / "memory", policy)
    memory.append_long_term(
        LongTermMemory(
            memory_id="summary-1",
            scope=MemoryScope.CONVERSATION,
            scope_id="conversation-1",
            conversation_id="conversation-1",
            kind=LongTermMemoryKind.SUMMARY,
            content="A very long summary that must be bounded before it reaches the model.",
        )
    )
    tool = memory_recall_tool(
        memory,
        conversation_id="conversation-1",
        policy=policy,
    )

    result = asyncio.run(
        tool.execute(
            ToolContext(execution_id="execution-1", agent_run_id="run-1"),
            {"query": "long summary"},
        )
    )

    item = result["items"][0]
    assert len(item["content"]) <= 4
    assert item["truncated"] is True


def test_compactor_creates_a_durable_handoff_for_old_tool_results(tmp_path: Path) -> None:
    messages = [
        Message(role=MessageRole.SYSTEM, content="system"),
        Message(
            role=MessageRole.TOOL,
            tool_call_id="call-old",
            content="old result " * 200,
        ),
    ]
    messages.extend(
        Message(role=MessageRole.USER, content=f"recent-{index}")
        if index % 2 == 0
        else Message(role=MessageRole.ASSISTANT, content=f"recent-{index}")
        for index in range(8)
    )
    request = LLMRequest(
        request_id="request-1",
        model="test-model",
        messages=tuple(messages),
    )
    budget = BudgetGuard(
        ContextBudgetPolicy(
            model_window_tokens=1_000,
            reserved_output_tokens=0,
            expected_growth_tokens=0,
            safety_margin_tokens=0,
            high_watermark=0.5,
            low_watermark=0.2,
        )
    )
    compacted = ContextCompactor(budget).compact(
        request,
        conversation_id="conversation-1",
        execution_id="execution-1",
        covered_until_sequence=12,
    )
    memory = JsonlMainAgentMemory(tmp_path / "memory")

    assert compacted.checkpoint is not None
    assert compacted.report.estimated_tokens < budget.check(request).estimated_tokens
    memory.append_checkpoint(compacted.checkpoint)
    assert memory.list_checkpoints("conversation-1") == (compacted.checkpoint,)
