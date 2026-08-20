"""Pure graph validation against the current capability Zoos."""

from collections import defaultdict, deque

from jsonschema import Draft202012Validator

from pleiad.graph.contracts import GraphDiagnostic, GraphSpec, GraphValidation
from pleiad.skills.zoo import SkillZoo
from pleiad.tools.zoo import ToolZoo


class GraphValidator:
    """Check graph shape, schemas, cycles, and capability references."""

    def __init__(self, tools: ToolZoo, skills: SkillZoo) -> None:
        self.tools = tools
        self.skills = skills

    def validate(self, graph: GraphSpec) -> GraphValidation:
        """Return all deterministic findings without side effects."""
        findings: list[GraphDiagnostic] = []
        for path, schema in (
            ("input_schema", graph.input_schema),
            ("output_schema", graph.output_schema),
        ):
            findings.extend(self._schema_findings(schema, path))
        for node in graph.nodes:
            findings.extend(
                self._schema_findings(node.input_schema, f"nodes.{node.node_id}.input_schema")
            )
            findings.extend(
                self._schema_findings(node.output_schema, f"nodes.{node.node_id}.output_schema")
            )
            for code in node.tool_codes:
                if code not in self.tools.codes():
                    findings.append(
                        GraphDiagnostic(code="unknown_tool", message=code, path=node.node_id)
                    )
            for code in node.skill_codes:
                try:
                    self.skills.resolve(code)
                except LookupError:
                    findings.append(
                        GraphDiagnostic(code="unknown_skill", message=code, path=node.node_id)
                    )
        findings.extend(self._cycle_findings(graph))
        return GraphValidation(valid=not findings, diagnostics=tuple(findings))

    @staticmethod
    def _schema_findings(schema: dict[str, object], path: str) -> list[GraphDiagnostic]:
        try:
            Draft202012Validator.check_schema(schema)
        except Exception as exc:
            return [GraphDiagnostic(code="invalid_schema", message=str(exc), path=path)]
        return []

    @staticmethod
    def _cycle_findings(graph: GraphSpec) -> list[GraphDiagnostic]:
        indegree = {node.node_id: 0 for node in graph.nodes}
        outgoing: dict[str, list[str]] = defaultdict(list)
        for edge in graph.edges:
            outgoing[edge.source_node_id].append(edge.target_node_id)
            indegree[edge.target_node_id] += 1
        queue = deque(node_id for node_id, value in indegree.items() if value == 0)
        visited = 0
        while queue:
            current = queue.popleft()
            visited += 1
            for target in outgoing[current]:
                indegree[target] -= 1
                if indegree[target] == 0:
                    queue.append(target)
        if visited == len(indegree):
            return []
        return [GraphDiagnostic(code="cycle", message="graph contains a cycle", path="edges")]
