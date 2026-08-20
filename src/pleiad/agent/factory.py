"""Assemble temporary Agents from graph-level codes and global registries."""

from pleiad.agent.contracts import AgentDefinition, AgentPolicy
from pleiad.agent.prompt import PromptBuilder
from pleiad.config import ModelRole
from pleiad.graph.contracts import NodeSpec
from pleiad.models import ModelRegistry
from pleiad.skills.zoo import SkillZoo
from pleiad.tools.zoo import ToolZoo


class AgentFactory:
    """Resolve Model, Skills, and Tools into a minimal Agent definition."""

    def __init__(self, models: ModelRegistry, tools: ToolZoo, skills: SkillZoo) -> None:
        self.models = models
        self.tools = tools
        self.skills = skills
        self.prompts = PromptBuilder()

    def for_node(self, node: NodeSpec, *, agent_id: str | None = None) -> AgentDefinition:
        """Build a Node Agent from a validated NodeSpec."""
        binding = self.models.resolve(node.model_role)
        tool_snapshot = self.tools.snapshot()
        skill_snapshot = self.skills.snapshot()
        selected_tool_codes = tuple(
            descriptor.code for descriptor in tool_snapshot.select(node.tool_codes)
        )
        selected_skill_codes = tuple(
            descriptor.code for descriptor in skill_snapshot.select(node.skill_codes)
        )
        return AgentDefinition(
            agent_id=agent_id or f"node-agent:{node.node_id}",
            model_role=node.model_role.value,
            client=binding.client,
            prompt=self.prompts.node_prompt(node),
            tools=self.tools.builtin_tools(),
            dynamic_tool_codes=tuple(
                code for code in selected_tool_codes if code not in self.tools.builtin_codes()
            ),
            dynamic_skill_codes=selected_skill_codes,
            output_schema=node.output_schema,
            tool_snapshot_id=tool_snapshot.snapshot_id,
            skill_snapshot_id=skill_snapshot.snapshot_id,
        )

    def custom(
        self,
        *,
        agent_id: str,
        role: ModelRole,
        prompt: str,
        tool_codes: tuple[str, ...] = (),
        skill_codes: tuple[str, ...] = (),
        output_schema: dict[str, object] | None = None,
        policy: AgentPolicy | None = None,
    ) -> AgentDefinition:
        """Build a non-graph Agent, primarily for Pleiad supervisor control."""
        binding = self.models.resolve(role)
        tool_snapshot = self.tools.snapshot()
        skill_snapshot = self.skills.snapshot()
        selected_tool_codes = tuple(
            descriptor.code for descriptor in tool_snapshot.select(tool_codes)
        )
        selected_skill_codes = tuple(
            descriptor.code for descriptor in skill_snapshot.select(skill_codes)
        )
        return AgentDefinition(
            agent_id=agent_id,
            model_role=role.value,
            client=binding.client,
            prompt=prompt,
            tools=self.tools.builtin_tools(),
            dynamic_tool_codes=tuple(
                code for code in selected_tool_codes if code not in self.tools.builtin_codes()
            ),
            dynamic_skill_codes=selected_skill_codes,
            output_schema=output_schema,
            policy=policy or AgentPolicy(),
            tool_snapshot_id=tool_snapshot.snapshot_id,
            skill_snapshot_id=skill_snapshot.snapshot_id,
        )
