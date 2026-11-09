import logging
from datetime import UTC, datetime

from pleiad.errors.base import ErrorType
from pleiad.sandbox.backends.oci import OciSandbox
from pleiad.sandbox.errors import SandboxCleanupError, SandboxExecutionError, SandboxUnavailableError
from pleiad.sandbox.models import SandboxObservation, SandboxReceipt
from pleiad.sessions.records import OperationRecord, ToolCallRecord
from pleiad.sessions.recovery import ActionEvidence, ActionRef
from pleiad.sessions.state import SessionState
from pleiad.tools.contracts import ToolAcceptance, ToolResult
from pydantic import ValidationError

from pleiad_weave.infrastructure.storage.product import fingerprint

logger = logging.getLogger(__name__)


async def observe_command_actions(
    backend: OciSandbox,
    journal: SessionState,
    run_id: str,
    *,
    cancel: bool,
    evidence: tuple[ActionEvidence, ...] = (),
) -> tuple[ActionEvidence, ...]:
    """Query original OCI identities and confirm requested cancellation without replay.

    Args:
        backend: Host-selected OCI adapter with daemon ownership verification.
        journal: Persisted original tool and operation records.
        run_id: SDK Run whose exclusive product ownership has been recovered.
        cancel: Whether the durable owner requested cancellation.
        evidence: Trusted host facts already supplied for specific original calls.

    Returns:
        Verified facts for matching command intentions. Missing or conflicting
        observations remain unsettled and require further host recovery.
    """
    operations = {value.operation_id: value for value in journal.operations if value.run_id == run_id}
    confirmed = {value.action.call_id for value in evidence if value.action.run_id == run_id}
    observations = list(evidence)
    for call in journal.tool_calls:
        if (
            call.run_id != run_id
            or call.call_id in confirmed
            or call.tool_code != "command"
            or call.operation_id is None
            or call.state not in {"prepared", "running", "unknown"}
        ):
            continue
        operation = operations.get(call.operation_id)
        if operation is None or operation.call_id != call.call_id:
            continue
        try:
            observed = await backend.query(call.operation_id)
            if not observation_matches(operation, observed):
                continue
            if cancel and observed.state in {"created", "running"} and observed.receipt is not None:
                original = observed.receipt
                observed = await backend.stop(original)
                if observed.receipt != original or observed.state not in {"cancelled", "succeeded", "failed"}:
                    continue
            value = command_evidence(call, operation, observed)
            if value is not None:
                observations.append(value)
        except (SandboxUnavailableError, SandboxExecutionError, SandboxCleanupError) as error:
            logger.warning(
                f"OCI recovery remains unsettled run_id={run_id} call_id={call.call_id} "
                f"operation_id={call.operation_id} code={error.code}"
            )
    return tuple(observations)


def observation_matches(operation: OperationRecord, observation: SandboxObservation) -> bool:
    """Require the daemon identity to preserve every saved backend acceptance field."""
    receipt = observation.receipt
    if (
        receipt is None
        or observation.state == "unknown"
        or observation.operation_id != operation.operation_id
        or receipt.operation_id != operation.operation_id
        or operation.submission is None
        or operation.submission.request_digest != receipt.request_digest
    ):
        return False
    acceptance = operation.backend_receipt
    if acceptance is None:
        return True
    try:
        original = SandboxReceipt.model_validate(acceptance.details)
    except ValidationError:
        return False
    return (
        original == receipt
        and acceptance.backend == receipt.runtime
        and acceptance.reference == receipt.container_id
        and acceptance.request_digest == receipt.request_digest
    )


def command_evidence(
    call: ToolCallRecord, operation: OperationRecord, observed: SandboxObservation
) -> ActionEvidence | None:
    """Translate verified process state while preserving the original action digests."""
    receipt = observed.receipt
    if receipt is None or observed.state == "unknown":
        return None
    state = "running" if observed.state == "created" else observed.state
    if state == "failed":
        state = "succeeded"
    now = datetime.now(UTC)
    reference = f"oci:{receipt.runtime}:{receipt.container_id}:{state}"
    result = None
    if state == "succeeded":
        result = ToolResult(
            call_id=call.call_id,
            ok=True,
            output={
                "exit_code": observed.exit_code,
                "backend": receipt.runtime,
                "backend_state": observed.state,
                "recovery_evidence_ref": reference,
            },
            started_at=now,
            finished_at=now,
        )
    elif state == "cancelled":
        result = ToolResult(
            call_id=call.call_id,
            ok=False,
            error_code=f"weave.command_recovered_{state}",
            error_message=(
                f"The OCI backend confirmed {state} for the original command. Exit code {observed.exit_code}."
            ),
            error_type=ErrorType.BUSINESS,
            started_at=now,
            finished_at=now,
        )
    acceptance = operation.backend_receipt
    if acceptance is None:
        acceptance = ToolAcceptance(
            operation_id=receipt.operation_id,
            backend=receipt.runtime,
            reference=receipt.container_id,
            request_digest=receipt.request_digest,
            details=receipt.model_dump(mode="json"),
        )
    return ActionEvidence(
        evidence_id=f"oci:{fingerprint(observed.model_dump(mode='json'))}",
        action=ActionRef(run_id=call.run_id, call_id=call.call_id, operation_id=operation.operation_id),
        binding_digest=call.binding_digest,
        arguments_digest=call.arguments_digest,
        backend_evidence_ref=reference,
        observed_at=now,
        observed_state=state,
        result_ref=reference if result is not None else None,
        result=result,
        acceptance=acceptance,
    )
