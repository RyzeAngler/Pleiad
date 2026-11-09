import asyncio
import json
from datetime import UTC, datetime

from pleiad.contracts import JsonObject
from pleiad.errors.base import PleiadError
from pleiad.messages.message import ToolMessage
from pleiad.orchestration.graph.node import Node
from pleiad.orchestration.graph.state import NodeResult, NodeStatus
from pleiad.runtime.checkpoint import ResumeDecision
from pleiad.sessions.checkpoint import RunCheckpoint

from pleiad_weave.application.errors import ErrorRecord
from pleiad_weave.infrastructure.storage.product_content import ProductContent
from pleiad_weave.infrastructure.storage.product_details import NodeAttempt, ProductDetails, ToolResultDetail


class NodeExecutor:
    """Persist attempts around SDK Node.run without replacing its lifecycle."""

    def __init__(self, execution_id: str, graph_run_id: str, details: ProductDetails, content: ProductContent) -> None:
        """Bind one real GraphRun to the common product journal and content owner."""
        self.execution_id = execution_id
        self.graph_run_id = graph_run_id
        self.details = details
        self.content = content

    async def execute(
        self,
        node: Node,
        input_value: JsonObject,
        *,
        checkpoint: RunCheckpoint | None = None,
        decisions: tuple[ResumeDecision, ...] = (),
    ) -> NodeResult:
        """Start or resume the original attempt and persist actual source identities."""
        record = await asyncio.to_thread(
            self.details.begin_node,
            self.execution_id,
            self.graph_run_id,
            node_id=node.node_id,
            input_value=input_value,
            model_name=None if node.agent is None else node.agent.model.model,
            checkpoint=checkpoint,
        )
        try:
            result = await node.run(input_value, checkpoint=checkpoint, decisions=decisions)
            artifact_ids: tuple[str, ...] = ()
            if result.output is not None:
                payload = json.dumps(result.output, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")
                artifact = await asyncio.to_thread(
                    self.content.install_artifact,
                    self.execution_id,
                    artifact_id=f"node:{record.node_attempt_id}:output",
                    name=f"node-{record.node_attempt_id}.json",
                    mime="application/json",
                    payload=payload,
                    graph_run_id=self.graph_run_id,
                    node_attempt_id=record.node_attempt_id,
                )
                artifact_ids = (artifact.artifact_id,)
            if result.session_id is not None:
                state = await node.session_store.snapshot(result.session_id)
                for entry in state.messages:
                    if isinstance(entry.message, ToolMessage):
                        await asyncio.to_thread(
                            self.details.save_tool_result,
                            ToolResultDetail(
                                tool_result_id=f"{entry.run_id}/{entry.message.message_id}",
                                execution_id=self.execution_id,
                                run_id=entry.run_id,
                                step_id=entry.step_id,
                                call_id=entry.message.tool_call_id,
                                text=entry.message.content,
                            ),
                        )
            error = None
            if result.error_code is not None and result.error_message is not None and result.error_type is not None:
                error = ErrorRecord(code=result.error_code, message=result.error_message, type=result.error_type)
            updated = record.model_copy(
                update={
                    "status": result.status,
                    "sdk_run_id": result.node_run_id if node.agent is not None else None,
                    "session_id": result.session_id,
                    "usage": result.usage,
                    "checkpoint": result.checkpoint,
                    "result": result.model_copy(update={"output": None}),
                    "artifact_ids": artifact_ids,
                    "error": error,
                    "finished_at": None if result.status is NodeStatus.PAUSED else datetime.now(UTC),
                }
            )
            await asyncio.to_thread(self.details.save_node, updated)
            return result
        except asyncio.CancelledError:
            await self.fail_attempt(record, None)
            raise
        except PleiadError as error:
            await self.fail_attempt(record, ErrorRecord.from_exception(error))
            raise

    async def fail_attempt(self, record: NodeAttempt, error: ErrorRecord | None) -> None:
        """Preserve unknown attempt status when its owning SDK call cannot confirm it."""
        await asyncio.to_thread(
            self.details.save_node,
            record.model_copy(update={"status": NodeStatus.UNKNOWN, "error": error, "finished_at": datetime.now(UTC)}),
        )
