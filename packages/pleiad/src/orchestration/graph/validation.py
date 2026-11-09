from collections import defaultdict, deque

from jsonschema import SchemaError

from pleiad.contracts import ContractModel
from pleiad.orchestration.graph.graph import Graph
from pleiad.orchestration.graph.mapping import pointer_tokens
from pleiad.orchestration.graph.state import NodeStatus
from pleiad.schemas import validate_json_schema


class GraphDiagnostic(ContractModel):
    """One deterministic Graph validation finding."""

    code: str
    message: str
    path: str = ""


class GraphValidation(ContractModel):
    """Validation result returned before execution begins."""

    valid: bool
    diagnostics: tuple[GraphDiagnostic, ...] = ()


class GraphValidator:
    """Validate schemas, references, DAG structure, and mapping conflicts."""

    def validate(self, graph: Graph) -> GraphValidation:
        """Return all deterministic findings without calling a node."""
        findings: list[GraphDiagnostic] = []
        findings.extend(self.schema_findings(graph))
        findings.extend(self.reference_findings(graph))
        findings.extend(self.cycle_findings(graph))
        findings.extend(self.mapping_findings(graph))
        return GraphValidation(valid=not findings, diagnostics=tuple(findings))

    @staticmethod
    def schema_findings(graph: Graph) -> list[GraphDiagnostic]:
        """Validate every public and node-local JSON Schema."""
        findings: list[GraphDiagnostic] = []
        schemas = [("input_schema", graph.input_schema), ("output_schema", graph.output_schema)]
        schemas.extend((f"nodes.{node.node_id}.input_schema", node.input_schema) for node in graph.nodes)
        schemas.extend((f"nodes.{node.node_id}.output_schema", node.output_schema) for node in graph.nodes)
        for path, schema in schemas:
            try:
                validate_json_schema(schema)
            except (SchemaError, ValueError) as error:
                findings.append(GraphDiagnostic(code="invalid_schema", message=str(error), path=path))
        return findings

    @staticmethod
    def reference_findings(graph: Graph) -> list[GraphDiagnostic]:
        """Validate unique IDs and all edge/output references."""
        findings: list[GraphDiagnostic] = []
        node_ids = [node.node_id for node in graph.nodes]
        if len(node_ids) != len(set(node_ids)):
            findings.append(GraphDiagnostic(code="duplicate_node", message="Node IDs must be unique", path="nodes"))
        edge_ids = [edge.edge_id for edge in graph.edges]
        if len(edge_ids) != len(set(edge_ids)):
            findings.append(GraphDiagnostic(code="duplicate_edge", message="Edge IDs must be unique", path="edges"))
        known = set(node_ids)
        for edge in graph.edges:
            if edge.source_node_id not in known or edge.target_node_id not in known:
                findings.append(
                    GraphDiagnostic(
                        code="dangling_edge",
                        message="Edge references an unknown Node",
                        path=edge.edge_id,
                    )
                )
        for output in graph.outputs:
            if output.source_node_id not in known:
                findings.append(
                    GraphDiagnostic(
                        code="dangling_output",
                        message="Output references an unknown Node",
                        path=output.source_node_id,
                    )
                )
        return findings

    @staticmethod
    def cycle_findings(graph: Graph) -> list[GraphDiagnostic]:
        """Reject cycles using deterministic Kahn traversal."""
        indegree = {node.node_id: 0 for node in graph.nodes}
        outgoing: dict[str, list[str]] = defaultdict(list)
        for edge in graph.edges:
            if edge.source_node_id in indegree and edge.target_node_id in indegree:
                indegree[edge.target_node_id] += 1
                outgoing[edge.source_node_id].append(edge.target_node_id)
        queue = deque(sorted(node_id for node_id, count in indegree.items() if count == 0))
        visited = 0
        while queue:
            node_id = queue.popleft()
            visited += 1
            for target in sorted(outgoing[node_id]):
                indegree[target] -= 1
                if indegree[target] == 0:
                    queue.append(target)
        if visited != len(indegree):
            return [GraphDiagnostic(code="cycle", message="Graph contains a cycle", path="edges")]
        return []

    @staticmethod
    def mapping_findings(graph: Graph) -> list[GraphDiagnostic]:
        """Reject duplicate or ancestor-overlapping writes into one Node input."""
        findings: list[GraphDiagnostic] = []
        for target in {edge.target_node_id for edge in graph.edges}:
            writes: list[tuple[tuple[str, ...], str]] = []
            for edge in graph.edges:
                if edge.target_node_id != target:
                    continue
                for mapping in edge.mappings:
                    try:
                        tokens = pointer_tokens(mapping.target_path)
                    except ValueError as error:
                        findings.append(GraphDiagnostic(code="invalid_mapping", message=str(error), path=edge.edge_id))
                        continue
                    writes.append((tokens, edge.edge_id))
            for index, (left, left_edge) in enumerate(writes):
                for right, right_edge in writes[index + 1 :]:
                    if left == right or left[: len(right)] == right or right[: len(left)] == left:
                        findings.append(
                            GraphDiagnostic(
                                code="mapping_conflict",
                                message="Graph mappings overlap an input field",
                                path=f"{left_edge},{right_edge}",
                            )
                        )
        return findings


def terminal_status(status: NodeStatus) -> bool:
    """Return whether a Node status has settled for dependency evaluation."""
    return status in {
        NodeStatus.SUCCEEDED,
        NodeStatus.FAILED,
        NodeStatus.SKIPPED,
        NodeStatus.UNKNOWN,
        NodeStatus.CANCELLED,
    }
