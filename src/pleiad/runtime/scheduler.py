"""Deterministic stage scheduler for DAG Nodes."""

from collections import defaultdict, deque

from pleiad.graph.contracts import EdgeSpec, GraphSpec, NodeSpec


def topological_stages(graph: GraphSpec) -> tuple[tuple[NodeSpec, ...], ...]:
    """Return independent node stages in deterministic order."""
    nodes = {node.node_id: node for node in graph.nodes}
    incoming = dict.fromkeys(nodes, 0)
    outgoing: dict[str, list[str]] = defaultdict(list)
    for edge in graph.edges:
        incoming[edge.target_node_id] += 1
        outgoing[edge.source_node_id].append(edge.target_node_id)
    stages: list[tuple[NodeSpec, ...]] = []
    ready = deque(sorted(node_id for node_id, count in incoming.items() if count == 0))
    visited: set[str] = set()
    while ready:
        current_ids = tuple(ready)
        ready.clear()
        current = tuple(nodes[node_id] for node_id in current_ids)
        stages.append(current)
        for node_id in current_ids:
            visited.add(node_id)
            for target in sorted(outgoing[node_id]):
                incoming[target] -= 1
                if incoming[target] == 0:
                    ready.append(target)
    if len(visited) != len(nodes):
        raise ValueError("cannot schedule a cyclic graph")
    return tuple(stages)


def edges_into(graph: GraphSpec, node_id: str) -> tuple[EdgeSpec, ...]:
    """Return incoming edges in stable order."""
    return tuple(
        sorted(
            (edge for edge in graph.edges if edge.target_node_id == node_id),
            key=lambda edge: edge.edge_id,
        )
    )
