from pydantic import Field, model_validator

from pleiad.contracts import ContractModel
from pleiad.messages.results import OperationResultReference
from pleiad.sessions.input import InputEnvelope, InputRecord
from pleiad.sessions.model_step import ModelStep
from pleiad.sessions.records import (
    EventRecord,
    OperationRecord,
    ReconcileRecord,
    RunSnapshot,
    RunStatus,
    SessionMessage,
    ToolCallRecord,
)


def operation_input_matches(operation: OperationRecord, envelope: InputEnvelope) -> bool:
    """Match a completion input to its original accepted operation and complete result."""
    return (
        envelope.input_id == f"operation:{operation.operation_id}:result"
        and envelope.kind == "operation_result"
        and envelope.source == "pleiad.operation"
        and envelope.causation_id == operation.operation_id
        and operation.state in {"succeeded", "failed", "cancelled"}
        and operation.backend_receipt is not None
        and operation.result is not None
        and envelope.content
        == OperationResultReference.from_result(operation.operation_id, operation.result).input_reference()
    )


class SessionState(ContractModel):
    """Validate the complete owned Session journal at each backend boundary."""

    session_id: str = Field(min_length=1, max_length=256)
    revision: int = Field(default=0, ge=0)
    writer_epoch: int = Field(default=0, ge=0)
    current_run_id: str | None = None
    inputs: tuple[InputRecord, ...] = ()
    runs: tuple[RunSnapshot, ...] = ()
    steps: tuple[ModelStep, ...] = ()
    messages: tuple[SessionMessage, ...] = ()
    events: tuple[EventRecord, ...] = ()
    tool_calls: tuple[ToolCallRecord, ...] = ()
    operations: tuple[OperationRecord, ...] = ()
    reconciliations: tuple[ReconcileRecord, ...] = ()

    @model_validator(mode="after")
    def validate_journal(self) -> "SessionState":
        """Reject duplicate identities, invalid ordering, and dangling ownership."""
        identities = (
            tuple(value.envelope.input_id for value in self.inputs),
            tuple(value.run_id for value in self.runs),
            tuple(value.invocation_id for value in self.runs),
            tuple(value.step_id for value in self.steps),
            tuple(value.message.message_id for value in self.messages),
            tuple(value.event_id for value in self.events),
            tuple(value.operation_id for value in self.operations),
            tuple((value.run_id, value.call_id) for value in self.tool_calls),
            tuple(value.request_id for value in self.reconciliations),
        )
        if any(len(values) != len(set(values)) for values in identities):
            raise ValueError("Session journal identities must be unique")
        for sequences in (
            tuple(value.sequence for value in self.inputs),
            tuple(value.sequence for value in self.steps),
            tuple(value.sequence for value in self.messages),
        ):
            if sequences != tuple(range(1, len(sequences) + 1)):
                raise ValueError("Session journal sequences must be continuous")
        run_ids = {value.run_id for value in self.runs}
        if self.current_run_id is not None and self.current_run_id not in run_ids:
            raise ValueError("Session active Run must exist")
        if any(value.session_id != self.session_id for value in self.runs):
            raise ValueError("Run Session identities must match their journal")
        if any(value.run_id not in run_ids for value in self.steps):
            raise ValueError("ModelSteps must belong to a persisted Run")
        current = tuple(
            value
            for value in self.runs
            if value.state in {RunStatus.RUNNING, RunStatus.WAITING_EVENT, RunStatus.PAUSED}
        )
        if len(current) > 1 or (current and current[0].run_id != self.current_run_id):
            raise ValueError("A Session must identify its only active or paused Run")
        if not current and self.current_run_id is not None:
            raise ValueError("Terminal Runs cannot hold the Session writer")
        if current and current[0].writer_epoch != self.writer_epoch:
            raise ValueError("The active Run must hold the Session writer epoch")
        inputs = {value.envelope.input_id: value for value in self.inputs}
        steps = {value.step_id: value for value in self.steps}
        for value in self.inputs:
            if value.run_id is not None and value.run_id not in run_ids:
                raise ValueError("Claimed inputs require an accepted Run")
            if value.step_id is not None:
                step = steps.get(value.step_id)
                if step is None or step.run_id != value.run_id or value.envelope.input_id not in step.input_ids:
                    raise ValueError("Consumed inputs require their matching model step")
        for step in self.steps:
            if any(identity not in inputs or inputs[identity].step_id != step.step_id for identity in step.input_ids):
                raise ValueError("ModelStep inputs must retain their original consumption facts")
            messages = tuple(value.message for value in self.messages if value.step_id == step.step_id)
            if messages != step.messages:
                raise ValueError("Message journal and ModelStep observations must agree")
        if any(value.step_id not in steps or steps[value.step_id].run_id != value.run_id for value in self.messages):
            raise ValueError("Messages must identify their original model step and Run")
        calls = {(value.run_id, value.call_id): value for value in self.tool_calls}
        for call in self.tool_calls:
            if call.run_id not in run_ids or call.step_id not in steps or steps[call.step_id].run_id != call.run_id:
                raise ValueError("Tool intentions must identify their original model step and Run")
        for operation in self.operations:
            original_call = calls.get((operation.run_id, operation.call_id))
            if (
                operation.run_id not in run_ids
                or original_call is None
                or original_call.operation_id != operation.operation_id
            ):
                raise ValueError("Operations must retain their original Run and tool-call ownership")
            if operation.result is not None and operation.result.call_id != original_call.call_id:
                raise ValueError("Operation result identity must match its original tool call")
        operations = {value.operation_id: value for value in self.operations}
        for value in self.inputs:
            if value.envelope.kind == "operation_result":
                causation = value.envelope.causation_id
                if causation is None:
                    raise ValueError("Operation inputs require their original operation identity")
                original_operation = operations.get(causation)
                if (
                    original_operation is None
                    or original_operation.run_id != value.run_id
                    or not operation_input_matches(original_operation, value.envelope)
                ):
                    raise ValueError("Operation inputs must reference their original immutable accepted result")
        for run in self.runs:
            attempts = run.model_attempts
            if len({value.attempt_id for value in attempts}) != len(attempts) or len(
                {value.step_id for value in attempts}
            ) != len(attempts):
                raise ValueError("Model attempt and step identities must be unique within a Run")
            for attempt in attempts:
                step = steps.get(attempt.step_id)
                if step is None or step.run_id != run.run_id:
                    raise ValueError("Model attempts must identify their original Run and step")
                if run.usage.model_calls <= attempt.usage_before.model_calls:
                    raise ValueError("Run usage must retain each admitted model call")
                charged_output = (
                    attempt.max_output_tokens
                    if attempt.result_usage is None or attempt.result_usage.output_tokens is None
                    else attempt.result_usage.output_tokens
                )
                if (
                    run.usage.budget_output_tokens - run.usage.child_budget_output_tokens
                    < attempt.usage_before.budget_output_tokens
                    - attempt.usage_before.child_budget_output_tokens
                    + charged_output
                ):
                    raise ValueError("Run usage must retain actual or conservative model charges")
            checkpoint = run.checkpoint
            if checkpoint is not None and (
                checkpoint.session_id != self.session_id
                or checkpoint.run_id != run.run_id
                or checkpoint.parent_run_id != run.parent_run_id
            ):
                raise ValueError("Run checkpoint ownership must match its persisted invocation")
            sequences = tuple(value.run_sequence for value in self.steps if value.run_id == run.run_id)
            if sequences != tuple(range(1, len(sequences) + 1)):
                raise ValueError("Run model-step sequences must be continuous")
            event_sequences = tuple(value.sequence for value in self.events if value.run_id == run.run_id)
            if event_sequences != tuple(range(1, len(event_sequences) + 1)):
                raise ValueError("Run event sequences must be continuous")
            owned = tuple(value.envelope.input_id for value in self.inputs if value.run_id == run.run_id)
            if run.input_ids != owned:
                raise ValueError("Run input ownership must agree with acceptance order")
        active = tuple(value for value in self.steps if value.state == "active")
        if len(active) > 1:
            raise ValueError("A Session can have only one active ModelStep")
        return self
