from collections.abc import Iterable

from pleiad.orchestration.graph.edge import Edge
from pleiad.orchestration.graph.graph import Graph
from pleiad.orchestration.graph.node import Node


def incoming_edges(graph: Graph, node_id: str) -> tuple[Edge, ...]:
    """Return incoming edges in stable declaration order."""
    return tuple(edge for edge in graph.edges if edge.target_node_id == node_id)


def outgoing_edges(graph: Graph, node_id: str) -> tuple[Edge, ...]:
    """Return outgoing edges in stable declaration order."""
    return tuple(edge for edge in graph.edges if edge.source_node_id == node_id)


def nodes_by_id(nodes: Iterable[Node]) -> dict[str, Node]:
    """Build the deterministic node lookup used by the executor."""
    return {node.node_id: node for node in nodes}
