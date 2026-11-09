import asyncio
from datetime import UTC, datetime

from pleiad.contracts import JsonObject
from pleiad.messages.message import AssistantMessage, ToolMessage
from pleiad.sessions.state import SessionState
from pydantic import TypeAdapter

from pleiad_weave.infrastructure.storage.product import fingerprint
from pleiad_weave.infrastructure.storage.product_content import ContentCorruptError, ProductContent
from pleiad_weave.infrastructure.storage.product_models import (
    ArtifactVerification,
    Execution,
    ProductConflict,
    ToolVerification,
    Verification,
)

JSON_OBJECT = TypeAdapter[JsonObject](JsonObject)


def verify_tool(requirement: ToolVerification, journal: SessionState) -> Verification:
    """Evaluate exact committed SDK call arguments and its actual completed result."""
    declarations = {
        (entry.run_id, call.id): JSON_OBJECT.validate_json(call.arguments)
        for entry in journal.messages
        if isinstance(entry.message, AssistantMessage)
        for call in entry.message.tool_calls
        if any(
            record.run_id == entry.run_id
            and record.call_id == call.id
            and record.tool_code == requirement.tool_code
            and record.state == "succeeded"
            for record in journal.tool_calls
        )
    }
    operations = {value.operation_id: value for value in journal.operations}
    references = {
        (entry.run_id, entry.message.tool_call_id): f"{entry.run_id}/{entry.message.message_id}"
        for entry in journal.messages
        if isinstance(entry.message, ToolMessage)
    }
    results: list[tuple[datetime, Verification]] = []
    for record in journal.tool_calls:
        if record.tool_code != requirement.tool_code:
            continue
        if declarations.get((record.run_id, record.call_id)) != requirement.arguments:
            continue
        result = record.result
        reference = references.get((record.run_id, record.call_id))
        if record.operation_id is not None:
            operation = operations.get(record.operation_id)
            if operation is None:
                continue
            result, reference = operation.result, operation.result_ref
        if result is None or reference is None:
            continue
        output = result.output
        passed = (
            result.ok
            and isinstance(output, dict)
            and fingerprint({key: output[key] for key in requirement.expected_output if key in output})
            == fingerprint(requirement.expected_output)
        )
        results.append(
            (
                result.finished_at,
                Verification(
                    name=requirement.name,
                    status="passed" if passed else "failed",
                    evidence_refs=(reference,),
                    executed_at=result.finished_at,
                    producer="host",
                ),
            )
        )
    if results:
        return max(results, key=lambda value: value[0])[1]
    return Verification(name=requirement.name, status="unknown", producer="host")


async def verify_delivery(
    execution: Execution, artifact_ids: tuple[str, ...], journal: SessionState, content: ProductContent
) -> tuple[Verification, ...]:
    """Execute every required host check independently of Worker completion claims."""
    results = []
    for requirement in execution.delivery_contract.required_verifications:
        if isinstance(requirement, ToolVerification):
            results.append(verify_tool(requirement, journal))
            continue
        if not isinstance(requirement, ArtifactVerification):
            raise TypeError("The delivery requirement has no host verification implementation")
        artifacts = [await asyncio.to_thread(content.store.get_artifact, identity) for identity in artifact_ids]
        matches = [
            value
            for value in artifacts
            if value is not None
            and value.name == requirement.artifact_name
            and value.owner_execution_id == execution.execution_id
        ]
        if len(matches) != 1:
            results.append(Verification(name=requirement.name, status="unknown", producer="host"))
            continue
        artifact = matches[0]
        try:
            await asyncio.to_thread(content.read_artifact, artifact.artifact_id)
            status = "passed"
        except (ContentCorruptError, ProductConflict):
            status = "failed"
        results.append(
            Verification(
                name=requirement.name,
                status=status,
                evidence_refs=(artifact.artifact_id,),
                executed_at=datetime.now(UTC),
                producer="host",
            )
        )
    return tuple(results)
