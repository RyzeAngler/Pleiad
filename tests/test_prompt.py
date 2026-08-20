"""Prompt contracts for Pleiad and dynamically generated execution nodes."""

import json

from pleiad.agent.prompt import NODE_SYSTEM_PROMPT, PLEIAD_SYSTEM_PROMPT, PromptBuilder
from pleiad.graph.contracts import NodeSpec
from pleiad.skills.contracts import Skill, SkillDescriptor


def test_pleiad_prompt_uses_runtime_capability_field_names() -> None:
    first_line = PLEIAD_SYSTEM_PROMPT.splitlines()[0]
    assert first_line.startswith("你是 Pleiad")
    assert "昴星团" in first_line
    assert "主 Agent" not in PLEIAD_SYSTEM_PROMPT
    assert "`code` 写入节点的 `tool_codes`" in PLEIAD_SYSTEM_PROMPT
    assert "`code` 写入节点的 `skill_codes`" in PLEIAD_SYSTEM_PROMPT


def test_node_prompt_keeps_current_objective_in_user_message() -> None:
    node = NodeSpec(
        node_id="summarize",
        objective="总结输入文档",
        prompt="保留原文中的日期。",
    )
    builder = PromptBuilder()

    system_prompt = builder.node_prompt(node)
    user_prompt = builder.user_prompt(
        node.objective,
        {"document": "正文"},
    )

    assert system_prompt.startswith(NODE_SYSTEM_PROMPT.strip())
    assert "总结输入文档" not in system_prompt
    assert '# 输出结构\n\n```json\n{\n  "type": "object"\n}\n```' in system_prompt
    assert "# 当前节点补充规则\n\n保留原文中的日期。" in system_prompt
    assert json.loads(user_prompt) == {
        "objective": "总结输入文档",
        "input": {"document": "正文"},
    }


def test_dynamic_skills_use_chinese_section_and_include_real_code() -> None:
    skill = Skill(
        descriptor=SkillDescriptor(
            name="资料核验",
            code="research.verify",
            description="核验资料来源",
        ),
        instructions="交叉检查关键事实。",
    )

    prompt = PromptBuilder().build("完成当前任务。", (skill,))

    assert "# 当前技能" in prompt
    assert "## 资料核验 · `research.verify`" in prompt
    assert prompt.endswith("交叉检查关键事实。")
