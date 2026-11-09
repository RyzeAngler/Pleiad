from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal, Self

from pleiad.contracts import ContractModel, JsonObject
from pleiad.sessions.recovery import ActionRef
from pydantic import AwareDatetime, Field, field_validator, model_validator

MAX_TARGET_EXECUTIONS = 20


class ProductConflict(ValueError):
    """Reject stale revisions, terminal writes, or conflicting idempotency keys."""

    def __init__(self, message: str, *, current_revision: int | None = None) -> None:
        super().__init__(message)
        self.current_revision = current_revision


class ProductCapacityError(ValueError):
    """Reject admission exceeding a configured product resource limit."""


class ExecutionStatus(StrEnum):
    """Describe the actual durable product work state."""

    QUEUED = "queued"
    RUNNING = "running"
    WAITING_INPUT = "waiting_input"
    WAITING_APPROVAL = "waiting_approval"
    WAITING_CHILDREN = "waiting_children"
    PAUSED = "paused"
    BLOCKED = "blocked"
    CANCELLING = "cancelling"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_STATUSES = frozenset({ExecutionStatus.SUCCEEDED, ExecutionStatus.FAILED, ExecutionStatus.CANCELLED})


class ContextReference(ContractModel):
    """Identify an authorized read-only product source."""

    kind: Literal["space", "execution", "artifact", "graph_node"]
    id: str = Field(min_length=1)
    execution_id: str | None = None
    graph_run_id: str | None = None
    node_id: str | None = None
    node_attempt_id: str | None = None

    @model_validator(mode="after")
    def validate_node_reference(self) -> Self:
        if self.kind == "graph_node" and any(
            value is None for value in (self.execution_id, self.graph_run_id, self.node_id)
        ):
            raise ValueError("A graph node reference requires its Execution, GraphRun, and node")
        return self


class ChatMessageRequest(ContractModel):
    """Accept one idempotent submission into the permanent main chat."""

    client_message_id: str = Field(min_length=1, max_length=256)
    text: str = Field(min_length=1)
    attachment_ids: tuple[str, ...] = Field(default=(), max_length=20)
    target_execution_ids: tuple[str, ...] = Field(default=(), max_length=MAX_TARGET_EXECUTIONS)
    reply_to_message_id: str | None = None
    context_refs: tuple[ContextReference, ...] = Field(default=(), max_length=20)

    @field_validator("text")
    @classmethod
    def validate_text(cls, value: str) -> str:
        if not value.strip() or len(value.encode("utf-8")) > 65_536:
            raise ValueError("Message text must be nonblank and at most 64 KiB")
        return value

    @model_validator(mode="after")
    def validate_unique_targets(self) -> Self:
        for values in (self.attachment_ids, self.target_execution_ids):
            if len(values) != len(set(values)):
                raise ValueError("Attachment and target identities must be unique")
        return self


class Message(ContractModel):
    """Persist one formally committed main-chat fact."""

    message_id: str
    chat_sequence: int = Field(ge=1)
    role: Literal["user", "assistant", "system"]
    body: str
    source_key: str
    created_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    attachment_ids: tuple[str, ...] = ()
    target_execution_ids: tuple[str, ...] = ()
    context_refs: tuple[ContextReference, ...] = ()
    reply_to_message_id: str | None = None
    report_ids: tuple[str, ...] = ()
    source_message_ids: tuple[str, ...] = ()


class MessageReceipt(ContractModel):
    """Confirm durable message acceptance and its per-target input identities."""

    message_id: str
    sequence: int
    accepted: Literal[True] = True
    input_ids: tuple[str, ...]


class SpaceConfiguration(ContractModel):
    """Describe a reusable Worker identity using host-owned capability references."""

    name: str = Field(min_length=1, max_length=160)
    purpose: str = Field(default="", max_length=4096)
    instructions: str = ""
    model_role: str = Field(default="text", min_length=1)
    tool_codes: tuple[str, ...] = ()
    skill_codes: tuple[str, ...] = ()
    resource_bindings: tuple[str, ...] = ()


class Space(SpaceConfiguration):
    """Persist one revisioned Space independently of its executions."""

    space_id: str
    revision: int = Field(default=1, ge=1)
    status: Literal["active", "archived"] = "active"
    deletion_pending: bool = False
    deleted: bool = False
    cleanup_generation: str | None = None
    created_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))


class SpaceView(Space):
    """Expose current admission actions and actual related-work aggregates."""

    available_actions: tuple[str, ...]
    disabled_reasons: dict[str, str]
    active_execution_count: int = Field(ge=0)
    pending_attention_count: int = Field(ge=0)


class ArtifactVerification(ContractModel):
    """Require the host to read and validate one named delivered artifact."""

    kind: Literal["artifact_integrity"] = "artifact_integrity"
    name: str = Field(min_length=1)
    artifact_name: str = Field(min_length=1)


class ToolVerification(ContractModel):
    """Bind a required check to exact executed arguments and observable result fields."""

    kind: Literal["tool_result"] = "tool_result"
    name: str = Field(min_length=1)
    tool_code: str = Field(min_length=1)
    arguments: JsonObject
    expected_output: JsonObject = Field(min_length=1)


type VerificationRequirement = Annotated[ArtifactVerification | ToolVerification, Field(discriminator="kind")]


class DeliveryContract(ContractModel):
    """Declare verifiable completion requirements for one real objective."""

    require_text: bool = True
    required_artifact_ids: tuple[str, ...] = ()
    required_verifications: tuple[VerificationRequirement, ...] = Field(default=(), max_length=20)
    required_children: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_verification_names(self) -> Self:
        """Require one unambiguous host check for every required name."""
        names = tuple(value.name for value in self.required_verifications)
        if len(names) != len(set(names)):
            raise ValueError("Required verification names must be unique")
        return self


class ExecutionSpec(ContractModel):
    """Describe actual work before assigning its product and Session identities."""

    space_id: str
    objective: str = Field(min_length=1)
    delivery_contract: DeliveryContract = Field(default_factory=DeliveryContract)
    budget_limit: int = Field(default=100_000, gt=0)
    parent_execution_id: str | None = None
    source_execution_id: str | None = None
    required: bool = True


class Execution(ExecutionSpec):
    """Persist the only durable product job and its immutable Worker snapshot."""

    execution_id: str
    session_id: str
    revision: int = Field(default=1, ge=1)
    status: ExecutionStatus = ExecutionStatus.QUEUED
    control_intent: Literal["none", "pause", "cancel"] = "none"
    reason: Literal["recovery_unknown", "resource_unavailable", "unhandled_failure"] | None = None
    config_snapshot: Space
    input_watermark: int = 0
    budget_consumed: int = Field(default=0, ge=0)
    budget_reserved: int = Field(default=0, ge=0)
    result: str | None = None
    unknown_effects: tuple[str, ...] = ()
    checkpoint_ref: str | None = None
    current_run_id: str | None = None
    run_attempt: int = 0
    validation_attempts: int = Field(default=0, ge=0)
    created_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))


class ExecutionView(Execution):
    """Project control eligibility without persisting derived UI state."""

    available_actions: tuple[str, ...]
    disabled_reasons: dict[str, str]
    pending_prerequisites: tuple[str, ...]
    pause_requested: bool


class InboxItem(ContractModel):
    """Keep immutable input payload apart from delivery and responsibility facts."""

    input_id: str
    origin_input_id: str
    target_kind: Literal["router", "execution"]
    target_id: str
    target_sequence: int
    kind: Literal["user", "observation", "delegate_result", "operation_result"]
    source: str
    content: str
    causation_id: str | None = None
    wake_policy: Literal["wake", "silent"]
    status: Literal["pending", "claimed", "handled"] = "pending"
    routing_revision: int = 1
    reroute_round: int = 0
    created_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))


class Delivery(ContractModel):
    """Persist an SDK delivery intent that can be retried using its original input."""

    delivery_id: str
    input_id: str
    target_kind: Literal["router", "execution"]
    target_id: str
    state: Literal["pending", "delivered", "failed"] = "pending"
    generation: int = 1
    attempts: int = 0
    sdk_sequence: int | None = None
    last_error: str | None = None
    next_due: AwareDatetime | None = None


class DeliveryView(Delivery):
    """Expose current retry eligibility independently of SDK consumption."""

    available_actions: tuple[str, ...]
    disabled_reasons: dict[str, str]


class InputConsumption(ContractModel):
    """Expose only SDK-confirmed acceptance, Run claim, and ModelStep consumption."""

    sequence: int = Field(ge=1)
    accepted_at: AwareDatetime
    state: Literal["accepted", "claimed", "consumed"]
    run_id: str | None
    step_id: str | None


class ExecutionInputView(InboxItem):
    """Join one immutable product input with its separately read SDK journal fact."""

    sdk_consumption: InputConsumption | None = None


class InputDisposition(ContractModel):
    """Track one target's responsibility independently of SDK input consumption."""

    origin_input_id: str
    target_execution_id: str
    input_id: str
    revision: int = 1
    required: bool = True
    state: Literal["owned", "handled", "returned"] = "owned"
    outcome_refs: tuple[str, ...] = ()
    handback_id: str | None = None


class InputDispositionView(InputDisposition):
    """Name a message's real responsibility owner independently of current active lists."""

    execution_objective: str
    space_name: str
    reassignment_pending: bool


class Handback(ContractModel):
    """Record the immutable remainder returned by one target exactly once."""

    handback_id: str
    origin_input_id: str
    target_execution_id: str
    input_id: str
    reason: str = Field(min_length=1)
    already_handled_refs: tuple[str, ...] = ()
    effect_refs: tuple[str, ...] = ()
    unknown_effects: bool = False
    run_id: str | None = None
    action_refs: tuple[ActionRef, ...] = ()
    reroute_round: int

    @model_validator(mode="after")
    def validate_unknown_origin(self) -> Self:
        """Bind an unknown-effect declaration to actual SDK action identities."""
        if self.unknown_effects and (self.run_id is None or not self.action_refs):
            raise ValueError("Unknown handback effects require their originating SDK actions")
        if any(value.run_id != self.run_id for value in self.action_refs):
            raise ValueError("Handback actions must belong to its original SDK Run")
        return self


class HandbackResolution(ContractModel):
    """Preserve a trusted settlement separately from the original unknown declaration."""

    handback_id: str
    run_id: str
    action_refs: tuple[ActionRef, ...] = Field(min_length=1)
    reconciliation_id: str
    evidence_ids: tuple[str, ...]
    reconciled_revision: int
    resolved_at: AwareDatetime


class SpaceReference(ContractModel):
    """Select an existing Space or one created within this routing decision."""

    kind: Literal["existing", "created"]
    id: str


class CreateSpaceAction(ContractModel):
    """Create a host-validated Worker identity inside a route transaction."""

    kind: Literal["create_space"] = "create_space"
    local_ref: str
    configuration: SpaceConfiguration


class DispatchAction(ContractModel):
    """Assign an independent real goal to a selected Worker."""

    kind: Literal["dispatch"] = "dispatch"
    space_ref: SpaceReference
    objective: str = Field(min_length=1)
    delivery_contract: DeliveryContract = Field(default_factory=DeliveryContract)
    source_input_ids: tuple[str, ...] = ()
    budget_limit: int = Field(default=100_000, gt=0)


class DeliverInputAction(ContractModel):
    """Assign an original request's remaining responsibility to a new target."""

    kind: Literal["deliver_input"] = "deliver_input"
    execution_id: str
    source_input_ids: tuple[str, ...]


class RequestCancelAction(ContractModel):
    """Record a user-requested cancellation without inventing completed cleanup."""

    kind: Literal["request_cancel"] = "request_cancel"
    execution_id: str


class AnswerQuestionAction(ContractModel):
    """Associate a user expression with a business question, never grant approval."""

    kind: Literal["answer_question"] = "answer_question"
    attention_id: str
    answer: str = Field(min_length=1)
    source_input_id: str = Field(min_length=1)


class RequestClarificationAction(ContractModel):
    """Keep an ambiguous request owned by a durable main-chat question."""

    kind: Literal["request_clarification"] = "request_clarification"
    question: str = Field(min_length=1, max_length=16_000)
    options: tuple[str, ...] = Field(default=(), max_length=20)
    source_input_ids: tuple[str, ...] = Field(min_length=1, max_length=20)


type RouteAction = Annotated[
    CreateSpaceAction
    | DispatchAction
    | DeliverInputAction
    | RequestCancelAction
    | AnswerQuestionAction
    | RequestClarificationAction,
    Field(discriminator="kind"),
]


class RouteDecision(ContractModel):
    """Validate the Router's candidate before applying any product effect."""

    decision_id: str
    source_input_ids: tuple[str, ...]
    reply: str = ""
    actions: tuple[RouteAction, ...] = Field(default=(), max_length=20)
    read_versions: dict[str, int] = Field(default_factory=dict)


class RouteAttempt(ContractModel):
    """Preserve stable product batch and distinct SDK inputs for every attempt."""

    decision_id: str
    attempt: int = 1
    invocation_id: str
    source_input_ids: tuple[str, ...]
    sdk_input_ids: tuple[str, ...]
    state: Literal["pending", "candidate", "applied", "failed"] = "pending"
    sdk_run_id: str | None = None
    candidate: RouteDecision | None = None
    failure_reason: str | None = None


class RouteReceipt(ContractModel):
    """Confirm atomic routing actions and the formally published reply."""

    decision_id: str
    attempt: int
    execution_ids: tuple[str, ...] = ()
    space_ids: tuple[str, ...] = ()
    attention_ids: tuple[str, ...] = ()
    message_id: str | None = None


class Verification(ContractModel):
    """Record an actual named check and evidence used by completion validation."""

    name: str
    status: Literal["passed", "failed", "unknown"]
    evidence_refs: tuple[str, ...] = ()
    executed_at: AwareDatetime | None = None
    producer: Literal["worker", "host"] = "worker"


class Report(ContractModel):
    """Accept a typed Worker report without treating prose as a terminal fact."""

    report_id: str
    execution_id: str
    run_id: str
    input_watermark: int = Field(ge=0)
    kind: Literal["progress", "needs_input", "ready", "failed"]
    summary: str
    artifact_ids: tuple[str, ...] = ()
    verification: tuple[Verification, ...] = ()
    unresolved: tuple[str, ...] = ()
    question: str | None = None
    rejection_reasons: tuple[str, ...] = ()


class ApprovalAction(ContractModel):
    """Show the exact saved SDK action before accepting an explicit approval."""

    tool_code: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    arguments: JsonObject


class Attention(ContractModel):
    """Persist a precise user decision with independent revision and expiry."""

    attention_id: str
    execution_id: str | None
    kind: Literal["clarification", "approval", "review", "recovery"]
    question: str = Field(min_length=1)
    options: tuple[str, ...] = ()
    action_digest: str | None = None
    action: ApprovalAction | None = None
    checkpoint_ref: str | None = None
    revision: int = 1
    status: Literal["pending", "resolved", "expired", "withdrawn"] = "pending"
    expires_at: AwareDatetime | None = None
    resolution: str | None = None
    resolution_source: str | None = None
    resolution_input_id: str | None = None
    source_input_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_approval(self) -> Self:
        if self.kind == "approval" and (
            self.action is None or self.action_digest is None or self.checkpoint_ref is None
        ):
            raise ValueError("An approval requires its exact action, digest, and checkpoint")
        return self


class AttentionView(Attention):
    """Expose the real owning work and eligibility of a current decision."""

    execution_objective: str | None
    space_name: str | None
    available_actions: tuple[str, ...]
    disabled_reasons: dict[str, str]


class ResolutionRequest(ContractModel):
    """Accept an explicit button decision or one atomic composer answer."""

    request_id: str
    expected_revision: int = Field(ge=1)
    answer: str | None = None
    decision: Literal["approve", "reject"] | None = None
    action_digest: str | None = None
    attachment_ids: tuple[str, ...] = Field(default=(), max_length=20)
    context_refs: tuple[ContextReference, ...] = Field(default=(), max_length=20)

    @model_validator(mode="after")
    def validate_answer(self) -> Self:
        if (self.answer is None) == (self.decision is None):
            raise ValueError("Exactly one answer or explicit decision is required")
        if self.answer is not None and (not self.answer.strip() or len(self.answer.encode()) > 65_536):
            raise ValueError("An answer must be nonblank and at most 64 KiB")
        return self


class ResolutionReceipt(ContractModel):
    """Return the same committed message and decision on a transmission retry."""

    message_id: str
    attention_revision: int
    execution: Execution | None


class Attachment(ContractModel):
    """Persist validated upload metadata and its temporary reference lifecycle."""

    attachment_id: str
    client_attachment_id: str
    name: str
    mime: str
    size: int
    digest: str
    relative_path: str
    status: Literal["ready", "referenced", "deleted"] = "ready"
    expires_at: AwareDatetime | None


class ArtifactMetadata(ContractModel):
    """Expose owned content facts without a private storage location."""

    artifact_id: str
    created_at: AwareDatetime
    name: str
    mime: str
    size: int
    digest: str
    owner_execution_id: str
    graph_run_id: str | None = None
    node_attempt_id: str | None = None
    previewable: bool = False
    downloadable: bool = True
    deletion_state: Literal["active", "deleting", "deleted", "corrupt"] = "active"
    cleanup_generation: str | None = None


class Artifact(ArtifactMetadata):
    """Persist checked content metadata and its private storage location."""

    relative_path: str


class ReportSummary(ContractModel):
    """Expose the actual Worker report linked by a formal chat message."""

    report_id: str
    execution_id: str
    kind: Literal["progress", "needs_input", "ready", "failed"]
    summary: str
    artifact_ids: tuple[str, ...]


class MessageView(Message):
    """Project durable report and content references in one chat read transaction."""

    reports: tuple[ReportSummary, ...] = ()
    artifacts: tuple[ArtifactMetadata, ...] = ()
    input_dispositions: tuple[InputDispositionView, ...] = ()
    handbacks: tuple[Handback, ...] = ()
    handback_resolutions: tuple[HandbackResolution, ...] = ()


class ProductEvent(ContractModel):
    """Persist one globally ordered committed product change."""

    sequence: int
    event_id: str
    subject: str
    type: str
    data: JsonObject
    created_at: AwareDatetime


class Page[T](ContractModel):
    """Bound one read transaction's ordered result and continuation cursor."""

    items: tuple[T, ...]
    total: int
    next_cursor: int | None
    read_sequence: int


class SequencePage[T](ContractModel):
    """Read newest-first records using an exclusive immutable sequence boundary."""

    items: tuple[T, ...]
    total: int
    next_before_sequence: int | None
    latest_sequence: int | None
    read_sequence: int


class RoutingStatus(ContractModel):
    """Project actual active Router work without publishing candidate reply text."""

    state: Literal["idle", "processing"]
    pending_input_count: int = Field(ge=0)


class Snapshot(ContractModel):
    """Return first-screen entities from the same SQLite read transaction."""

    snapshot_sequence: int
    routing: RoutingStatus
    messages: Page[MessageView]
    spaces: Page[SpaceView]
    executions: Page[ExecutionView]
    attentions: Page[AttentionView]
    capabilities: JsonObject


class CleanupScope(ContractModel):
    """Bind controlled SDK, task-file, and fact impact without exposing their private bodies."""

    sdk_session_count: int = Field(default=0, ge=0)
    task_file_count: int = Field(default=0, ge=0)
    space_fact_count: int = Field(default=0, ge=0)
    digest: str = ""
    blockers: tuple[str, ...] = ()


class DeletionPreview(ContractModel):
    """Bind an exact deletion impact to a Space revision and expiry."""

    space_id: str
    space_revision: int
    preview_token: str
    expires_at: AwareDatetime
    execution_ids: tuple[str, ...]
    artifact_ids: tuple[str, ...]
    shared_artifact_ids: tuple[str, ...]
    blockers: tuple[str, ...]
    reference_digest: str
    scopes: CleanupScope = Field(default_factory=CleanupScope)


class CleanupRecord(ContractModel):
    """Fence accepted asynchronous cleanup using one durable generation."""

    request_id: str
    space_id: str
    generation: str
    plan: DeletionPreview
    session_ids: tuple[str, ...] = ()
    completed_stages: tuple[Literal["sessions_planned", "sessions", "facts", "work", "details", "content"], ...] = ()
    state: Literal["pending", "failed", "completed"] = "pending"
    last_error: str | None = None


class BudgetReservation(ContractModel):
    """Reserve a parent's allowance until an independent child is settled."""

    reservation_id: str
    parent_execution_id: str
    child_execution_id: str
    amount: int = Field(gt=0)
    consumed: int | None = Field(default=None, ge=0)
    settled: bool = False


class ExecutionRun(ContractModel):
    """Associate one SDK invocation with product work without another job."""

    execution_id: str
    attempt: int
    invocation_id: str
    run_id: str | None = None
    state: Literal["pending", "running", "paused", "succeeded", "failed", "cancelled"] = "pending"
    checkpoint_ref: str | None = None
    usage: JsonObject = Field(default_factory=dict)
    charged_budget_output_tokens: int = Field(default=0, ge=0)


class ExecutionRunPage(SequencePage[ExecutionRun]):
    """Identify the newest invocation's SDK Run independently of the displayed page."""

    latest_run_id: str | None


class ExecutionDetailView(ExecutionView):
    """Read product ownership atomically and identify the separate SDK journal revision."""

    read_sequence: int = Field(ge=0)
    session_read_sequence: int = Field(ge=0)
    reports: tuple[Report, ...]
    children: tuple[ExecutionView, ...]
    artifacts: tuple[ArtifactMetadata, ...]
    attentions: tuple[AttentionView, ...]
    runs: tuple[ExecutionRun, ...]
    inputs: tuple[ExecutionInputView, ...]
    deliveries: tuple[DeliveryView, ...]
    dispositions: tuple[InputDisposition, ...]
    handbacks: tuple[Handback, ...]
    handback_resolutions: tuple[HandbackResolution, ...]
