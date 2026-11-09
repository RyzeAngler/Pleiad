from collections.abc import Mapping

from pleiad.agent.agent import Agent
from pleiad.context.selection.models import ContextBlock
from pleiad.contracts import SkillRef, ToolRef
from pleiad.models.anthropic.client import AnthropicModelClient
from pleiad.models.openai.client import OpenAIModelClient
from pleiad.orchestration.graph.graph import Graph
from pleiad.orchestration.graph.node import Node
from pleiad.runtime.runner import Runner
from pleiad.sessions.store import SessionStore

from pleiad_weave.agent.contracts import GraphSpec
from pleiad_weave.agent.prompt import build_node_prompt


def build_graph(
    spec: GraphSpec,
    models: Mapping[str, OpenAIModelClient | AnthropicModelClient],
    runner: Runner,
    *,
    session_store: SessionStore,
    context_blocks: tuple[ContextBlock, ...] = (),
) -> Graph:
    """Bind an owned graph definition to public SDK Agent execution mechanisms.

    Args:
        spec: Validated immutable product graph definition.
        models: Host-selected model-role bindings.
        runner: SDK runner with currently authorized tools and skills.
        session_store: SDK backend for independently recoverable node Sessions.
        context_blocks: Authorized references supplied to each node.

    Returns:
        An SDK graph with node approval and recovery handled by the SDK.

    Raises:
        LookupError: If a capability or model role is unavailable.
    """
    nodes: list[Node] = []
    for node in spec.nodes:
        try:
            model = models[node.model_role]
        except KeyError as error:
            raise LookupError(f"Model role is not configured: {node.model_role}") from error
        agent = Agent(
            name=f"node:{node.node_id}",
            model=model,
            prompt=build_node_prompt(node),
            tools=tuple(ToolRef(code) for code in node.tool_codes),
            skills=tuple(SkillRef(code) for code in node.skill_codes),
            output_schema=node.output_schema,
        )
        nodes.append(
            Node(
                node_id=node.node_id,
                input_schema=node.input_schema,
                output_schema=node.output_schema,
                agent=agent,
                runner=runner,
                limits=node.limits,
                session_store=session_store,
                context_blocks=context_blocks,
            )
        )
    return Graph(
        graph_id=spec.graph_id,
        revision=spec.revision,
        objective=spec.objective,
        nodes=tuple(nodes),
        edges=spec.edges,
        outputs=spec.outputs,
        input_schema=spec.input_schema,
        output_schema=spec.output_schema,
        max_concurrency=spec.max_concurrency,
        timeout_seconds=spec.timeout_seconds,
    )
