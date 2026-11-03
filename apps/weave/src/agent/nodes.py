import asyncio
from datetime import UTC, datetime
from uuid import uuid4

from pleiad.contracts import JsonObject
from pleiad.errors.base import PleiadError, SystemFault
from pleiad.orchestration.graph.node import Node
from pleiad.orchestration.graph.state import NodeResult, NodeStatus

from pleiad_weave.application.errors import ErrorRecord
from pleiad_weave.application.events import ExecutionEventPublisher
from pleiad_weave.infrastructure.storage.artifacts import ArtifactStore
from pleiad_weave.infrastructure.storage.models import NodeRunRecord
from pleiad_weave.infrastructure.storage.repository import NodeRunRepository


class NodeExecutor:
    """Persist product attempts around the SDK's executable Node boundary."""

    def __init__(
        self,
        execution_id: str,
        graph_id: str,
        revision: int,
        records: NodeRunRepository,
        artifacts: ArtifactStore,
        publisher: ExecutionEventPublisher,
    ) -> None:
        """Bind product identity without replacing SDK Agent execution."""
        self.execution_id = execution_id
        self.graph_id = graph_id
        self.revision = revision
        self.records = records
        self.artifacts = artifacts
        self.publisher = publisher

    async def execute(self, node: Node, input_value: JsonObject) -> NodeResult:
        """Save the attempt lifecycle and delegate all execution to Node.run."""
        record = NodeRunRecord(
            node_run_id=uuid4().hex,
            execution_id=self.execution_id,
            graph_id=self.graph_id,
            graph_revision=self.revision,
            node_id=node.node_id,
            status=NodeStatus.RUNNING,
        )
        await asyncio.to_thread(self.records.save, record)
        try:
            result = await node.run(input_value)
            artifact = (
                None if result.output is None else await asyncio.to_thread(self.artifacts.put_json, result.output)
            )
            error = None
            if result.error_code is not None and result.error_message is not None and result.error_type is not None:
                error = ErrorRecord(code=result.error_code, message=result.error_message, type=result.error_type)
            await asyncio.to_thread(
                self.records.save,
                record.model_copy(
                    update={
                        "status": result.status,
                        "sdk_run_id": result.node_run_id if node.agent is not None else None,
                        "sdk_run_status": result.run_status,
                        "usage": result.usage,
                        "artifact_id": None if artifact is None else artifact.artifact_id,
                        "error": error,
                        "finished_at": datetime.now(UTC),
                    }
                ),
            )
        except asyncio.CancelledError:
            await asyncio.to_thread(
                self.records.save,
                record.model_copy(update={"status": NodeStatus.UNKNOWN, "finished_at": datetime.now(UTC)}),
            )
            raise
        except PleiadError as failure:
            await asyncio.to_thread(
                self.records.save,
                record.model_copy(
                    update={
                        "status": NodeStatus.UNKNOWN,
                        "error": ErrorRecord.from_exception(failure),
                        "finished_at": datetime.now(UTC),
                    }
                ),
            )
            raise
        except Exception as failure:
            fault = SystemFault("weave.node_unexpected", f"Node execution failed: {type(failure).__name__}.")
            await asyncio.to_thread(
                self.records.save,
                record.model_copy(
                    update={
                        "status": NodeStatus.UNKNOWN,
                        "error": ErrorRecord.from_exception(fault),
                        "finished_at": datetime.now(UTC),
                    }
                ),
            )
            raise fault from failure
        await self.publisher.emit(
            self.execution_id,
            "node.completed",
            {
                "node_id": node.node_id,
                "node_run_id": record.node_run_id,
                "sdk_run_id": result.node_run_id if node.agent is not None else None,
                "sdk_run_status": None if result.run_status is None else result.run_status.value,
                "usage": None if result.usage is None else result.usage.model_dump(mode="json"),
                "node_status": result.status.value,
                "error": None if error is None else error.model_dump(mode="json"),
            },
        )
        return result
