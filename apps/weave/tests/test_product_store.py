import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier

import pytest
from pleiad.errors.base import SystemFault

from pleiad_weave.infrastructure.storage.product import ProductStore, stable_id
from pleiad_weave.infrastructure.storage.product_content import ContentCorruptError, ProductContent
from pleiad_weave.infrastructure.storage.product_models import (
    AnswerQuestionAction,
    ApprovalAction,
    Attention,
    ChatMessageRequest,
    ContextReference,
    CreateSpaceAction,
    DeliveryContract,
    DispatchAction,
    Execution,
    ExecutionRun,
    ExecutionSpec,
    ExecutionStatus,
    ProductCapacityError,
    ProductConflict,
    Report,
    RequestClarificationAction,
    ResolutionRequest,
    RouteDecision,
    SpaceConfiguration,
    SpaceReference,
    ToolVerification,
    Verification,
)


@pytest.fixture
def store(tmp_path: Path) -> ProductStore:
    return ProductStore(tmp_path / "runtime-v2")


def create_execution(store: ProductStore, identity: str, *, running: bool = False) -> Execution:
    space = store.create_space(SpaceConfiguration(name="Research", purpose="Find sources"), request_id="research")
    execution = store.create_execution(
        ExecutionSpec(space_id=space.space_id, objective=f"Work {identity}"), request_id=identity
    )
    if not running:
        return execution
    execution = store.transition_execution(
        execution.execution_id, ExecutionStatus.RUNNING, expected_revision=execution.revision
    )
    store.record_execution_run(
        ExecutionRun(
            execution_id=execution.execution_id,
            attempt=1,
            invocation_id=f"{identity}/1",
            run_id=f"run-{identity}",
            state="running",
        )
    )
    current = store.get_execution(execution.execution_id)
    assert current is not None
    return current


def test_retried_main_chat_message_is_accepted_once_and_changed_payload_conflicts(store: ProductStore) -> None:
    request = ChatMessageRequest(client_message_id="submit", text="Find two reports")
    first = store.submit_message(request)
    reopened = ProductStore(store.root)

    assert reopened.submit_message(request) == first
    assert reopened.messages().total == 1
    assert reopened.get_input(first.input_ids[0]) is not None
    with pytest.raises(ProductConflict, match="different content"):
        reopened.submit_message(request.model_copy(update={"text": "Find another report"}))
    assert reopened.messages().total == 1


def test_a_terminal_explicit_target_rejects_the_entire_multi_target_message(store: ProductStore) -> None:
    one = create_execution(store, "one")
    two = create_execution(store, "two")
    store.transition_execution(two.execution_id, ExecutionStatus.FAILED, expected_revision=two.revision)

    with pytest.raises(ProductConflict, match="cannot accept"):
        store.submit_message(
            ChatMessageRequest(
                client_message_id="multi",
                text="Use the new deadline",
                target_execution_ids=(one.execution_id, two.execution_id),
            )
        )

    assert store.messages().total == 0
    assert store.pending_deliveries() == ()


def test_one_route_can_create_two_real_root_jobs_with_independent_sessions(store: ProductStore) -> None:
    space = store.create_space(SpaceConfiguration(name="Research"), request_id="research")
    store.submit_message(
        ChatMessageRequest(client_message_id="request", text="Investigate Alpha and Beta independently")
    )
    batch = store.claim_router_batch()
    assert batch is not None
    candidate = RouteDecision(
        decision_id=batch.decision_id,
        source_input_ids=batch.source_input_ids,
        reply="Both investigations have started",
        read_versions={f"space:{space.space_id}": 1},
        actions=tuple(
            DispatchAction(space_ref=SpaceReference(kind="existing", id=space.space_id), objective=goal)
            for goal in ("Investigate Alpha", "Investigate Beta")
        ),
    )
    store.save_route_candidate(candidate, attempt=1, sdk_run_id="router-run")

    receipt = store.apply_route_decision(batch.decision_id, attempt=1)

    assert store.apply_route_decision(batch.decision_id, attempt=1) == receipt
    executions = store.list_executions().items
    assert {value.objective for value in executions} == {"Investigate Alpha", "Investigate Beta"}
    assert all(value.parent_execution_id is None for value in executions)
    assert len({value.session_id for value in executions}) == 2
    assert store.messages().total == 2
    assert store.claim_router_batch() is None


def test_invalid_later_route_action_rolls_back_created_spaces_and_reply(store: ProductStore) -> None:
    store.submit_message(ChatMessageRequest(client_message_id="request", text="Start work"))
    batch = store.claim_router_batch()
    assert batch is not None
    candidate = RouteDecision(
        decision_id=batch.decision_id,
        source_input_ids=batch.source_input_ids,
        reply="Work started",
        actions=(
            CreateSpaceAction(local_ref="new", configuration=SpaceConfiguration(name="New")),
            DispatchAction(space_ref=SpaceReference(kind="created", id="missing"), objective="Run"),
        ),
    )
    store.save_route_candidate(candidate, attempt=1, sdk_run_id="router")

    with pytest.raises(ProductConflict, match="not defined"):
        store.apply_route_decision(batch.decision_id, attempt=1)

    assert store.list_spaces().total == 0
    assert store.list_executions().total == 0
    assert store.messages().total == 1


def test_a_route_cannot_assign_a_request_while_asking_for_its_missing_requirement(store: ProductStore) -> None:
    space = store.create_space(SpaceConfiguration(name="Research"), request_id="space")
    store.submit_message(ChatMessageRequest(client_message_id="request", text="Prepare a brief"))
    batch = store.claim_router_batch()
    assert batch is not None
    store.save_route_candidate(
        RouteDecision(
            decision_id=batch.decision_id,
            source_input_ids=batch.source_input_ids,
            reply="Which audience?",
            read_versions={f"space:{space.space_id}": space.revision},
            actions=(
                RequestClarificationAction(question="Which audience?", source_input_ids=batch.source_input_ids),
                DispatchAction(space_ref=SpaceReference(kind="existing", id=space.space_id), objective="Write a brief"),
            ),
        ),
        attempt=batch.attempt,
        sdk_run_id="router",
    )

    with pytest.raises(ProductConflict, match="while asking for clarification"):
        store.apply_route_decision(batch.decision_id, attempt=batch.attempt)

    assert store.list_executions().total == 0
    assert store.attentions().total == 0
    assert store.messages().total == 1


def test_object_revision_conflict_uses_a_new_sdk_observation_without_reclaiming_source(store: ProductStore) -> None:
    space = store.create_space(SpaceConfiguration(name="Research"), request_id="space")
    store.submit_message(ChatMessageRequest(client_message_id="request", text="Research Alpha"))
    batch = store.claim_router_batch()
    assert batch is not None
    candidate = RouteDecision(
        decision_id=batch.decision_id,
        source_input_ids=batch.source_input_ids,
        read_versions={f"space:{space.space_id}": 1},
        actions=(
            DispatchAction(space_ref=SpaceReference(kind="existing", id=space.space_id), objective="Research Alpha"),
        ),
    )
    store.save_route_candidate(candidate, attempt=1, sdk_run_id="router")
    store.update_space(space.space_id, SpaceConfiguration(name="Changed"), request_id="update", expected_revision=1)

    with pytest.raises(ProductConflict, match="revision changed"):
        store.apply_route_decision(batch.decision_id, attempt=1)
    second = store.reassess_route(batch.decision_id, reason="Space revision changed")

    assert second.source_input_ids == batch.source_input_ids
    assert second.sdk_input_ids == (f"{batch.decision_id}/2/reassessment",)
    assert set(second.sdk_input_ids).isdisjoint(batch.sdk_input_ids)
    assert second.invocation_id != batch.invocation_id
    assert store.messages().total == 1


def test_router_links_a_main_chat_answer_to_its_real_message_without_republishing_user_text(
    store: ProductStore,
) -> None:
    execution = create_execution(store, "one", running=True)
    question = store.create_attention(
        Attention(
            attention_id="audience",
            execution_id=execution.execution_id,
            kind="clarification",
            question="Which audience?",
        )
    )
    accepted = store.submit_message(ChatMessageRequest(client_message_id="answer", text="The engineering team"))
    batch = store.claim_router_batch()
    assert batch is not None
    store.save_route_candidate(
        RouteDecision(
            decision_id=batch.decision_id,
            source_input_ids=batch.source_input_ids,
            read_versions={f"attention:{question.attention_id}": question.revision},
            actions=(
                AnswerQuestionAction(
                    attention_id=question.attention_id,
                    answer="The engineering team",
                    source_input_id=accepted.input_ids[0],
                ),
            ),
        ),
        attempt=batch.attempt,
        sdk_run_id="router-answer",
    )

    receipt = store.apply_route_decision(batch.decision_id, attempt=batch.attempt)

    assert store.apply_route_decision(batch.decision_id, attempt=batch.attempt) == receipt
    assert [(value.message_id, value.body) for value in store.messages().items] == [
        (accepted.message_id, "The engineering team")
    ]
    resolved = store.get_attention(question.attention_id)
    assert resolved is not None and resolved.status == "resolved"
    assert resolved.resolution_source == accepted.message_id
    assigned = store.worker_inputs(execution.execution_id)
    assert len(assigned) == 1
    assert assigned[0].causation_id == accepted.message_id
    assert '"question": "Which audience?"' in assigned[0].content
    assert '"answer": "The engineering team"' in assigned[0].content


def test_progress_reports_keep_router_silent_while_details_persist(store: ProductStore) -> None:
    execution = create_execution(store, "one", running=True)
    for index in range(100):
        report = Report(
            report_id=f"progress-{index}",
            execution_id=execution.execution_id,
            run_id="run-one",
            input_watermark=0,
            kind="progress",
            summary=f"Progress {index}",
        )
        assert store.accept_report(report) == report
        assert store.accept_report(report) == report

    assert len(store.reports(execution.execution_id)) == 100
    assert store.claim_router_batch() is None
    assert store.messages().total == 0
    assert store.get_execution(execution.execution_id) == execution


def test_ready_requires_real_input_disposition_and_wakes_only_after_valid_completion(store: ProductStore) -> None:
    execution = create_execution(store, "one", running=True)
    receipt = store.submit_message(
        ChatMessageRequest(
            client_message_id="followup",
            text="Include the new requirement",
            target_execution_ids=(execution.execution_id,),
        )
    )
    item = store.get_input(receipt.input_ids[0])
    assert item is not None
    report = Report(
        report_id="premature",
        execution_id=execution.execution_id,
        run_id="run-one",
        input_watermark=item.target_sequence,
        kind="ready",
        summary="The report includes the requirement",
    )

    rejected = store.accept_report(report)

    assert rejected.rejection_reasons
    assert store.claim_router_batch() is None
    valid = store.accept_report(report.model_copy(update={"report_id": "ready"}), handled_input_ids=(item.input_id,))
    assert valid.rejection_reasons == ()
    final = store.get_execution(execution.execution_id)
    assert final is not None and final.status is ExecutionStatus.SUCCEEDED
    assert store.claim_router_batch() is not None
    disposition = store.get_disposition(item.origin_input_id, execution.execution_id)
    assert disposition is not None and disposition.state == "handled"


def test_handback_waits_for_all_target_responsibilities_before_router_wake(store: ProductStore) -> None:
    one = create_execution(store, "one", running=True)
    two = create_execution(store, "two", running=True)
    message = store.submit_message(
        ChatMessageRequest(
            client_message_id="followup",
            text="Investigate the remaining issue",
            target_execution_ids=(one.execution_id, two.execution_id),
        )
    )
    first, second = (store.get_input(identity) for identity in message.input_ids)
    assert first is not None and second is not None

    returned = store.return_to_router(
        first.input_id, execution_id=one.execution_id, expected_revision=1, reason="Outside my scope"
    )
    assert (
        store.return_to_router(
            first.input_id, execution_id=one.execution_id, expected_revision=1, reason="Outside my scope"
        )
        == returned
    )
    assert store.claim_router_batch() is None
    evidence = store.accept_report(
        Report(
            report_id="evidence",
            execution_id=two.execution_id,
            run_id="run-two",
            input_watermark=second.target_sequence,
            kind="progress",
            summary="The second part was answered",
        )
    )
    store.handle_input(
        second.input_id, execution_id=two.execution_id, expected_revision=1, outcome_refs=(evidence.report_id,)
    )

    batch = store.claim_router_batch()
    assert batch is not None
    assert any(
        item.source == "weave.handback"
        for identity in batch.source_input_ids
        if (item := store.get_input(identity)) is not None
    )
    disposition = store.get_disposition(second.origin_input_id, two.execution_id)
    assert disposition is not None and disposition.state == "handled"
    projected = store.messages().items[0]
    returned_view = next(
        value for value in projected.input_dispositions if value.target_execution_id == one.execution_id
    )
    assert returned_view.reassignment_pending
    assert returned_view.execution_objective == one.objective
    assert returned_view.space_name == "Research"
    assert projected.handbacks == (returned,)


def test_worker_claim_cannot_satisfy_a_required_host_check_with_an_arbitrary_artifact(store: ProductStore) -> None:
    space = store.create_space(SpaceConfiguration(name="Checks"), request_id="space")
    execution = store.create_execution(
        ExecutionSpec(
            space_id=space.space_id,
            objective="Run unit tests",
            delivery_contract=DeliveryContract(
                required_verifications=(
                    ToolVerification(
                        name="unit_tests",
                        tool_code="command",
                        arguments={"command": "python -m pytest"},
                        expected_output={"exit_code": 0},
                    ),
                )
            ),
        ),
        request_id="work",
    )
    execution = store.transition_execution(
        execution.execution_id, ExecutionStatus.RUNNING, expected_revision=execution.revision
    )
    store.record_execution_run(
        ExecutionRun(
            execution_id=execution.execution_id, attempt=1, invocation_id="work/1", run_id="run", state="running"
        )
    )
    artifact = ProductContent(store).install_artifact(
        execution.execution_id, artifact_id="claim", name="claim.txt", mime="text/plain", payload=b"Tests passed"
    )

    report = store.accept_report(
        Report(
            report_id="ready",
            execution_id=execution.execution_id,
            run_id="run",
            input_watermark=0,
            kind="ready",
            summary="Tests passed",
            artifact_ids=(artifact.artifact_id,),
            verification=(
                Verification(
                    name="unit_tests",
                    status="passed",
                    evidence_refs=(artifact.artifact_id,),
                    executed_at=datetime.now(UTC),
                ),
            ),
        )
    )

    assert "Required verification evidence is incomplete" in report.rejection_reasons
    current = store.get_execution(execution.execution_id)
    assert current is not None and current.status is ExecutionStatus.RUNNING


def test_answer_during_user_pause_is_atomic_and_resume_recomputes_approval_wait(store: ProductStore) -> None:
    execution = create_execution(store, "one")
    paused = store.control_execution(
        execution.execution_id, "pause", request_id="pause", expected_revision=execution.revision
    )
    question = store.create_attention(
        Attention(
            attention_id="question", execution_id=execution.execution_id, kind="clarification", question="Which region?"
        )
    )
    store.create_attention(
        Attention(
            attention_id="approval",
            execution_id=execution.execution_id,
            kind="approval",
            action=ApprovalAction(
                tool_code="command", tool_name="command", arguments={"shell_command": "echo checked"}
            ),
            question="Approve writing?",
            action_digest="action",
            checkpoint_ref="checkpoint",
        )
    )
    request = ResolutionRequest(request_id="answer", expected_revision=question.revision, answer="North")

    receipt = store.resolve_attention(question.attention_id, request)

    assert store.resolve_attention(question.attention_id, request) == receipt
    assert receipt.execution is not None and receipt.execution.status is ExecutionStatus.PAUSED
    assert receipt.execution.control_intent == "pause"
    assert store.messages().total == 2
    with pytest.raises(ProductConflict, match="different content"):
        store.resolve_attention(question.attention_id, request.model_copy(update={"answer": "South"}))
    resumed = store.control_execution(
        execution.execution_id, "resume", request_id="resume", expected_revision=receipt.execution.revision
    )
    assert resumed.status is ExecutionStatus.WAITING_APPROVAL
    assert resumed.control_intent == "none"
    assert paused.status is ExecutionStatus.PAUSED


def test_expired_approval_cannot_authorize_a_model_claim_or_late_button(store: ProductStore) -> None:
    execution = create_execution(store, "one", running=True)
    attention = store.create_attention(
        Attention(
            attention_id="approval",
            execution_id=execution.execution_id,
            kind="approval",
            action=ApprovalAction(
                tool_code="command", tool_name="command", arguments={"shell_command": "echo checked"}
            ),
            question="Approve writing?",
            action_digest="action",
            checkpoint_ref="checkpoint",
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
        )
    )

    with pytest.raises(ProductConflict, match="expired"):
        store.resolve_attention(
            attention.attention_id,
            ResolutionRequest(request_id="late", expected_revision=1, decision="approve", action_digest="action"),
        )

    current = store.get_attention(attention.attention_id)
    assert current is not None and current.status == "pending"
    assert store.messages().total == 0


def test_ready_and_new_targeted_input_have_one_consistent_admission_order(store: ProductStore) -> None:
    execution = create_execution(store, "one", running=True)
    barrier = Barrier(2)

    def complete() -> bool:
        barrier.wait(timeout=5)
        report = store.accept_report(
            Report(
                report_id="ready",
                execution_id=execution.execution_id,
                run_id="run-one",
                input_watermark=0,
                kind="ready",
                summary="Complete",
            )
        )
        return not report.rejection_reasons

    def supplement() -> bool:
        barrier.wait(timeout=5)
        try:
            store.submit_message(
                ChatMessageRequest(
                    client_message_id="racing", text="New requirement", target_execution_ids=(execution.execution_id,)
                )
            )
        except ProductConflict:
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as executor:
        completion = executor.submit(complete)
        admission = executor.submit(supplement)
        completed, admitted = completion.result(), admission.result()

    assert completed != admitted
    assert store.messages().total == int(admitted)
    current = store.get_execution(execution.execution_id)
    assert current is not None
    assert (current.status is ExecutionStatus.SUCCEEDED) == completed


def test_child_budget_reservation_and_settlement_do_not_refund_twice(store: ProductStore) -> None:
    parent = create_execution(store, "parent")
    child = store.create_execution(
        ExecutionSpec(
            space_id=parent.space_id, objective="Child", parent_execution_id=parent.execution_id, budget_limit=30_000
        ),
        request_id="child",
    )
    reservation = store.reserve_budget(parent.execution_id, child.execution_id, 30_000)
    assert store.settle_budget(reservation.reservation_id, None) == reservation
    settled = store.settle_budget(reservation.reservation_id, 10_000)

    assert store.settle_budget(reservation.reservation_id, 10_000) == settled
    current = store.get_execution(parent.execution_id)
    assert current is not None
    assert (current.budget_consumed, current.budget_reserved) == (10_000, 0)
    with pytest.raises(ProductCapacityError):
        store.create_execution(
            ExecutionSpec(
                space_id=parent.space_id,
                objective="Too large",
                parent_execution_id=parent.execution_id,
                budget_limit=95_000,
            ),
            request_id="overdraw",
        )
    assert store.list_executions().total == 2


def test_snapshot_pages_share_the_same_committed_event_high_watermark(store: ProductStore) -> None:
    execution = create_execution(store, "one")
    store.submit_message(
        ChatMessageRequest(client_message_id="request", text="Update", target_execution_ids=(execution.execution_id,))
    )

    snapshot = store.snapshot(limit=1)

    assert {
        snapshot.messages.read_sequence,
        snapshot.spaces.read_sequence,
        snapshot.executions.read_sequence,
        snapshot.attentions.read_sequence,
    } == {snapshot.snapshot_sequence}
    assert snapshot.messages.items[0].body == "Update"
    assert snapshot.executions.items[0].execution_id == execution.execution_id
    assert store.events().items[-1].sequence == snapshot.snapshot_sequence


def test_incompatible_product_database_is_rejected_without_schema_mutation(tmp_path: Path) -> None:
    root = tmp_path / "runtime-v2"
    root.mkdir()
    path = root / "product.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE conversations(id TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO conversations VALUES('old')")

    with pytest.raises(ValueError, match="Unsupported product"):
        ProductStore(root)

    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT id FROM conversations").fetchall() == [("old",)]
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0


def test_uploaded_content_retry_preserves_one_reference_and_rejects_changed_bytes(store: ProductStore) -> None:
    content = ProductContent(store)
    first = content.upload_attachment(
        client_attachment_id="upload", name="notes.txt", mime="text/plain", payload=b"real notes"
    )

    assert (
        content.upload_attachment(
            client_attachment_id="upload", name="notes.txt", mime="text/plain", payload=b"real notes"
        )
        == first
    )
    assert content.read_attachment(first.attachment_id) == b"real notes"
    with pytest.raises(ProductConflict, match="different content"):
        content.upload_attachment(
            client_attachment_id="upload", name="notes.txt", mime="text/plain", payload=b"changed notes"
        )
    assert content.read_attachment(first.attachment_id) == b"real notes"


def test_upload_rejects_bytes_inconsistent_with_the_declared_document_type(store: ProductStore) -> None:
    content = ProductContent(store)

    with pytest.raises(ValueError, match="declared MIME"):
        content.upload_attachment(
            client_attachment_id="bad", name="image.png", mime="image/png", payload=b"not an image"
        )

    assert store.get_attachment(stable_id("attachment/upload/bad")) is None


def test_formal_message_reference_wins_against_staging_expiry_and_prevents_revocation(store: ProductStore) -> None:
    content = ProductContent(store)
    attachment = content.upload_attachment(
        client_attachment_id="upload", name="notes.txt", mime="text/plain", payload=b"formal evidence"
    )
    store.submit_message(
        ChatMessageRequest(
            client_message_id="request", text="Use this evidence", attachment_ids=(attachment.attachment_id,)
        )
    )

    assert content.expire_attachments(now=datetime.now(UTC) + timedelta(days=2)) == ()
    assert content.read_attachment(attachment.attachment_id) == b"formal evidence"
    with pytest.raises(ProductConflict, match="formally referenced"):
        content.delete_attachment(attachment.attachment_id, request_id="revoke")


def test_attachment_expiry_and_formal_reference_commit_one_winning_lifecycle(store: ProductStore) -> None:
    content = ProductContent(store)
    attachment = content.upload_attachment(
        client_attachment_id="upload", name="notes.txt", mime="text/plain", payload=b"evidence"
    )
    barrier = Barrier(2)

    def expire() -> tuple[str, ...]:
        barrier.wait(timeout=5)
        return content.expire_attachments(now=datetime.now(UTC) + timedelta(days=2))

    def refer() -> bool:
        barrier.wait(timeout=5)
        try:
            store.submit_message(
                ChatMessageRequest(
                    client_message_id="request", text="Use evidence", attachment_ids=(attachment.attachment_id,)
                )
            )
        except ProductConflict:
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as executor:
        expiry = executor.submit(expire)
        reference = executor.submit(refer)
        expired, referenced = expiry.result(), reference.result()

    assert bool(expired) != referenced
    assert store.messages().total == int(referenced)
    if referenced:
        assert content.read_attachment(attachment.attachment_id) == b"evidence"
    else:
        with pytest.raises(ProductConflict, match=r"expired|deleted"):
            content.read_attachment(attachment.attachment_id)


def test_changed_artifact_bytes_become_corrupt_and_block_ready_settlement(store: ProductStore) -> None:
    execution = create_execution(store, "one", running=True)
    content = ProductContent(store)
    artifact = content.install_artifact(
        execution.execution_id, artifact_id="result", name="result.txt", mime="text/plain", payload=b"actual result"
    )
    (store.content_root / artifact.relative_path).write_bytes(b"corrupt result")

    with pytest.raises(ContentCorruptError, match="integrity"):
        content.read_artifact(artifact.artifact_id)
    report = store.accept_report(
        Report(
            report_id="ready",
            execution_id=execution.execution_id,
            run_id="run-one",
            input_watermark=0,
            kind="ready",
            summary="Complete",
            artifact_ids=(artifact.artifact_id,),
        )
    )

    assert report.rejection_reasons
    current = store.get_execution(execution.execution_id)
    assert current is not None and current.status is ExecutionStatus.RUNNING
    damaged = store.get_artifact(artifact.artifact_id)
    assert damaged is not None and damaged.deletion_state == "corrupt"


def test_new_reference_invalidates_space_deletion_preview(store: ProductStore) -> None:
    space = store.create_space(SpaceConfiguration(name="Research"), request_id="space")
    content = ProductContent(store)
    preview = content.deletion_preview(space.space_id)
    store.submit_message(
        ChatMessageRequest(
            client_message_id="reference",
            text="Use this Space",
            context_refs=(ContextReference(kind="space", id=space.space_id),),
        )
    )

    with pytest.raises(ProductConflict, match="impact changed"):
        content.accept_deletion(
            space.space_id, request_id="delete", expected_revision=1, preview_token=preview.preview_token
        )

    current = store.get_space(space.space_id)
    assert current is not None and not current.deletion_pending


def test_accepted_space_cleanup_rejects_new_references_and_reclaims_its_content_once(store: ProductStore) -> None:
    execution = create_execution(store, "one", running=True)
    content = ProductContent(store)
    artifact = content.install_artifact(
        execution.execution_id, artifact_id="result", name="result.txt", mime="text/plain", payload=b"owned output"
    )
    current = store.get_execution(execution.execution_id)
    assert current is not None
    store.transition_execution(execution.execution_id, ExecutionStatus.FAILED, expected_revision=current.revision)
    preview = content.deletion_preview(execution.space_id)
    cleanup = content.accept_deletion(
        execution.space_id, request_id="delete", expected_revision=1, preview_token=preview.preview_token
    )

    with pytest.raises(ProductConflict, match="unavailable"):
        store.submit_message(
            ChatMessageRequest(
                client_message_id="late",
                text="Reference output",
                context_refs=(ContextReference(kind="artifact", id=artifact.artifact_id),),
            )
        )
    with pytest.raises(ProductConflict, match="obsolete"):
        content.reclaim_content(cleanup.request_id, expected_generation="old-generation")
    completed = content.reclaim_content(cleanup.request_id, expected_generation=cleanup.generation)

    assert "content" in completed.completed_stages
    with pytest.raises(ProductConflict, match="every owned cleanup stage"):
        content.finish_deletion(cleanup.request_id, expected_generation=cleanup.generation)
    assert content.reclaim_content(cleanup.request_id, expected_generation=cleanup.generation) == completed
    assert not (store.content_root / artifact.relative_path).exists()
    tombstone = store.get_space(execution.space_id)
    assert tombstone is not None and tombstone.deletion_pending and not tombstone.deleted
    assert not any(event.type == "space.deleted" for event in store.events().items)
    assert store.messages().total == 0


def test_shared_artifact_reference_survives_owning_space_cleanup(store: ProductStore) -> None:
    execution = create_execution(store, "one", running=True)
    content = ProductContent(store)
    artifact = content.install_artifact(
        execution.execution_id, artifact_id="result", name="result.txt", mime="text/plain", payload=b"shared output"
    )
    store.submit_message(
        ChatMessageRequest(
            client_message_id="share",
            text="Retain this output",
            context_refs=(ContextReference(kind="artifact", id=artifact.artifact_id),),
        )
    )
    current = store.get_execution(execution.execution_id)
    assert current is not None
    store.transition_execution(execution.execution_id, ExecutionStatus.FAILED, expected_revision=current.revision)
    preview = content.deletion_preview(execution.space_id)
    assert preview.shared_artifact_ids == (artifact.artifact_id,)
    cleanup = content.accept_deletion(
        execution.space_id, request_id="delete", expected_revision=1, preview_token=preview.preview_token
    )

    content.reclaim_content(cleanup.request_id, expected_generation=cleanup.generation)

    assert content.read_artifact(artifact.artifact_id) == b"shared output"
    assert store.messages().items[0].context_refs[0].id == artifact.artifact_id


def test_snapshot_projects_current_actions_counts_and_waits_from_one_product_read(store: ProductStore) -> None:
    execution = create_execution(store, "one", running=True)
    store.create_attention(
        Attention(
            attention_id="approval",
            execution_id=execution.execution_id,
            kind="approval",
            action=ApprovalAction(
                tool_code="command", tool_name="command", arguments={"shell_command": "echo checked"}
            ),
            question="Approve the exact write?",
            action_digest="action",
            checkpoint_ref="checkpoint",
        )
    )

    snapshot = store.snapshot()

    assert snapshot.capabilities["max_target_executions"] == 20
    assert snapshot.spaces.items[0].active_execution_count == 1
    assert snapshot.spaces.items[0].pending_attention_count == 1
    assert "archive" in snapshot.spaces.items[0].available_actions
    assert snapshot.executions.items[0].pending_prerequisites == ("approval",)
    assert "cancel" in snapshot.executions.items[0].available_actions
    assert "supplement" in snapshot.executions.items[0].available_actions
    assert snapshot.attentions.items[0].available_actions == ("resolve",)
    assert snapshot.attentions.items[0].execution_objective == execution.objective
    assert snapshot.attentions.items[0].space_name == "Research"
    current = store.get_execution(execution.execution_id)
    assert current is not None
    store.control_execution(execution.execution_id, "cancel", request_id="cancel", expected_revision=current.revision)
    updated = store.snapshot()
    assert updated.executions.items[0].available_actions == ()
    assert updated.attentions.items[0].available_actions == ()
    assert "resolve" in updated.attentions.items[0].disabled_reasons


def test_resuming_one_sdk_run_charges_only_new_usage_and_rejects_usage_regression(store: ProductStore) -> None:
    execution = create_execution(store, "cumulative", running=True)
    first = store.settle_run_usage(execution.execution_id, invocation_id="cumulative/1", consumed=100)
    repeated = store.settle_run_usage(execution.execution_id, invocation_id="cumulative/1", consumed=100)
    resumed = store.settle_run_usage(execution.execution_id, invocation_id="cumulative/1", consumed=140)

    assert first.budget_consumed == repeated.budget_consumed == 100
    assert resumed.budget_consumed == 140
    assert store.execution_runs(execution.execution_id)[0].charged_budget_output_tokens == 140
    with pytest.raises(ProductConflict, match="backwards"):
        store.settle_run_usage(execution.execution_id, invocation_id="cumulative/1", consumed=120)


def test_a_failed_required_child_wakes_the_parent_to_consume_its_failure_and_refunds_the_reservation(
    store: ProductStore,
) -> None:
    parent = create_execution(store, "parent", running=True)
    child = store.create_execution(
        ExecutionSpec(
            space_id=parent.space_id,
            objective="Required child",
            parent_execution_id=parent.execution_id,
            budget_limit=30_000,
        ),
        request_id="child",
    )
    current = store.get_execution(parent.execution_id)
    assert current is not None
    store.transition_execution(
        parent.execution_id, ExecutionStatus.WAITING_CHILDREN, expected_revision=current.revision
    )

    store.transition_execution(child.execution_id, ExecutionStatus.FAILED, expected_revision=child.revision)

    awakened = store.get_execution(parent.execution_id)
    assert awakened is not None and awakened.status is ExecutionStatus.QUEUED
    assert awakened.budget_reserved == 0
    result = store.worker_inputs(parent.execution_id)[0]
    assert result.kind == "delegate_result" and result.causation_id == child.execution_id
    assert '"status":"failed"' in result.content
    assert store.list_executions().items[0].pending_prerequisites == ()


def test_an_invalid_nested_request_rolls_back_the_entire_checkpoint_attention_projection(store: ProductStore) -> None:
    execution = create_execution(store, "one", running=True)
    run = store.execution_runs(execution.execution_id)[0]
    assert run.run_id is not None
    store.record_execution_run(run.model_copy(update={"state": "paused", "checkpoint_ref": run.run_id}))
    before = store.get_execution(execution.execution_id)
    one = Attention(
        attention_id="one",
        execution_id=execution.execution_id,
        kind="clarification",
        question="First question?",
        checkpoint_ref=run.run_id,
    )
    two = one.model_copy(update={"attention_id": "two", "checkpoint_ref": "foreign-run"})

    with pytest.raises(ProductConflict, match="another checkpoint owner"):
        store.project_pending_attentions(
            execution.execution_id, run.run_id, attentions=(one, two), has_interruptions=True
        )

    assert store.get_execution(execution.execution_id) == before
    assert store.attentions(status=None).items == ()


@pytest.mark.parametrize("mime", ["text/plain\r\nX-Injected: yes", "", "text"])
def test_artifact_publication_rejects_invalid_response_media_types(store: ProductStore, mime: str) -> None:
    execution = create_execution(store, "media")
    with pytest.raises(ValueError, match="MIME type"):
        ProductContent(store).install_artifact(
            execution.execution_id, artifact_id="unsafe-media", name="result.txt", mime=mime, payload=b"Output"
        )
    assert store.get_artifact("unsafe-media") is None


class ReadOnlyProductStore(ProductStore):
    def connect(self) -> sqlite3.Connection:
        """Inject a real SQLite read-only connection at the external storage boundary."""
        connection = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        return connection


def test_storage_readiness_rejects_a_real_read_only_database_even_when_its_read_queries_work(
    store: ProductStore,
) -> None:
    readonly = ReadOnlyProductStore(store.root)
    assert readonly.events().items == ()

    with pytest.raises(SystemFault) as failure:
        readonly.probe_writable()

    assert failure.value.code == "weave.product_storage"
    assert readonly.messages().items == ()


def test_rejected_delivery_corrections_preserve_input_ownership_and_stop_after_three_attempts(
    tmp_path: Path,
) -> None:
    store = ProductStore(tmp_path / "runtime-v2", queue_capacity=1)
    execution = create_execution(store, "correction", running=True)
    receipt = store.submit_message(
        ChatMessageRequest(
            client_message_id="work", text="Required input", target_execution_ids=(execution.execution_id,)
        )
    )
    item = store.get_input(receipt.input_ids[0])
    assert item is not None
    for index in range(4):
        run = store.execution_runs(execution.execution_id)[-1]
        store.record_execution_run(run.model_copy(update={"state": "succeeded"}))
        inputs = store.worker_inputs(execution.execution_id, pending_only=False)
        assert run.run_id is not None
        report = Report(
            report_id=f"candidate-{index}",
            execution_id=execution.execution_id,
            run_id=run.run_id,
            input_watermark=max(value.target_sequence for value in inputs),
            kind="ready",
            summary="Unverified result",
            rejection_reasons=("The proposed evidence is unavailable",),
        )
        rejected = store.accept_report(report, handled_input_ids=(item.input_id,))
        assert rejected.rejection_reasons == ("The proposed evidence is unavailable",)
        disposition = store.get_disposition(item.origin_input_id, execution.execution_id)
        assert disposition is not None and disposition.state == "owned"
        corrected = store.queue_after_rejected_report(report.report_id, evidence_refs=())
        assert store.queue_after_rejected_report(report.report_id, evidence_refs=()) == corrected
        if index < 3:
            assert corrected.status is ExecutionStatus.QUEUED and corrected.validation_attempts == index + 1
            running = store.transition_execution(
                execution.execution_id, ExecutionStatus.RUNNING, expected_revision=corrected.revision
            )
            store.record_execution_run(
                ExecutionRun(
                    execution_id=execution.execution_id,
                    attempt=index + 2,
                    invocation_id=f"correction/{index + 2}",
                    run_id=f"run-{index + 2}",
                    state="running",
                )
            )
            assert running.status is ExecutionStatus.RUNNING
        else:
            assert corrected.status is ExecutionStatus.FAILED and corrected.validation_attempts == 3
    observations = [
        value
        for value in store.worker_inputs(execution.execution_id, pending_only=False)
        if value.source == "weave.validation"
    ]
    assert len(observations) == 3 and all(value.status == "handled" for value in observations)
    assert store.reports(execution.execution_id)[-1].kind == "failed"
    disposition = store.get_disposition(item.origin_input_id, execution.execution_id)
    assert disposition is not None and disposition.state == "returned"


@pytest.mark.parametrize("outcome", ["report", "handback"])
def test_already_accepted_outcomes_wake_a_full_silent_router_without_reopening_external_admission(
    tmp_path: Path, outcome: str
) -> None:
    store = ProductStore(tmp_path / "runtime-v2", queue_capacity=1)
    execution = create_execution(store, "work", running=True)
    receipt = store.submit_message(
        ChatMessageRequest(
            client_message_id="assigned", text="Accepted work", target_execution_ids=(execution.execution_id,)
        )
    )
    item = store.get_input(receipt.input_ids[0])
    assert item is not None
    if outcome == "report":
        accepted = store.accept_report(
            Report(
                report_id="ready",
                execution_id=execution.execution_id,
                run_id="run-work",
                input_watermark=item.target_sequence,
                kind="ready",
                summary="Delivered",
            ),
            handled_input_ids=(item.input_id,),
        )
        assert not accepted.rejection_reasons
        expected = ExecutionStatus.SUCCEEDED
    else:
        store.transition_execution(execution.execution_id, ExecutionStatus.FAILED, expected_revision=execution.revision)
        expected = ExecutionStatus.FAILED
    terminal = store.get_execution(execution.execution_id)
    assert terminal is not None and terminal.status is expected
    batch = store.claim_router_batch()
    assert batch is not None and len(batch.source_input_ids) == 2
    inputs = [store.get_input(identity) for identity in batch.source_input_ids]
    assert all(value is not None for value in inputs)
    assert any(value is not None and value.source == f"weave.{outcome}" for value in inputs)
    with pytest.raises(ProductCapacityError):
        store.submit_message(ChatMessageRequest(client_message_id="overflow", text="New external request"))
    assert store.messages().total == 1


def test_a_declared_outcome_source_cannot_reserve_capacity_without_its_persisted_producer(store: ProductStore) -> None:
    with pytest.raises(ProductConflict, match="durable producer identity"), store.transaction(write=True) as connection:
        store.enqueue(
            connection,
            input_id=stable_id("report-input/missing"),
            origin_input_id=stable_id("report-input/missing"),
            target_kind="router",
            target_id="router",
            content="Forged result",
            source="weave.report",
            kind="delegate_result",
            causation_id="missing",
            wake_policy="wake",
        )
    assert store.get_input(stable_id("report-input/missing")) is None


def test_failed_delivery_retry_keeps_its_input_and_stops_when_responsibility_is_returned(store: ProductStore) -> None:
    execution = create_execution(store, "delivery")
    receipt = store.submit_message(
        ChatMessageRequest(
            client_message_id="delivery", text="Inspect this input", target_execution_ids=(execution.execution_id,)
        )
    )
    input_id = receipt.input_ids[0]
    delivery = next(item for item in store.pending_deliveries() if item.input_id == input_id)
    failed = store.fail_delivery(delivery.delivery_id, expected_generation=delivery.generation, error="SDK unavailable")
    with store.transaction() as connection:
        assert store.delivery_view(connection, failed).available_actions == ("retry",)
    retried = store.retry_delivery(failed.delivery_id, request_id="retry", expected_generation=failed.generation)
    assert retried.input_id == input_id and retried.generation == failed.generation + 1
    assert (
        store.retry_delivery(failed.delivery_id, request_id="retry", expected_generation=failed.generation) == retried
    )
    failed_again = store.fail_delivery(
        retried.delivery_id, expected_generation=retried.generation, error="SDK unavailable"
    )
    store.return_to_router(input_id, execution_id=execution.execution_id, expected_revision=1, reason="Outside scope")
    with store.transaction() as connection:
        view = store.delivery_view(connection, failed_again)
        assert view.available_actions == () and "retry" in view.disabled_reasons
    with pytest.raises(ProductConflict, match="no longer permits"):
        store.retry_delivery(failed.delivery_id, request_id="late-retry", expected_generation=failed_again.generation)
    assert store.get_input(input_id) is not None


def test_router_processing_snapshot_keeps_candidate_text_private_until_atomic_application(store: ProductStore) -> None:
    assert store.snapshot().routing.state == "idle"
    store.submit_message(ChatMessageRequest(client_message_id="router-indicator", text="What is happening?"))
    queued = store.snapshot()
    assert queued.routing.state == "processing" and queued.routing.pending_input_count == 1
    batch = store.claim_router_batch()
    assert batch is not None
    store.save_route_candidate(
        RouteDecision(decision_id=batch.decision_id, source_input_ids=batch.source_input_ids, reply="Committed answer"),
        attempt=1,
        sdk_run_id="router-run",
    )
    pending = store.snapshot()
    assert pending.routing.state == "processing" and pending.routing.pending_input_count == 1
    assert [message.body for message in pending.messages.items] == ["What is happening?"]
    store.apply_route_decision(batch.decision_id, attempt=1)
    applied = store.snapshot()
    assert applied.routing.state == "idle" and applied.routing.pending_input_count == 0
    assert [message.body for message in applied.messages.items] == ["What is happening?", "Committed answer"]


def test_retry_rejects_a_space_changed_after_validation_without_consuming_the_retry_intent(store: ProductStore) -> None:
    source = create_execution(store, "retry-source")
    source = store.transition_execution(source.execution_id, ExecutionStatus.FAILED, expected_revision=source.revision)
    original = store.get_space(source.space_id)
    assert original is not None
    changed = store.update_space(
        source.space_id,
        SpaceConfiguration(name="Current Worker", instructions="Current method"),
        request_id="change-before-retry",
        expected_revision=original.revision,
    )
    with pytest.raises(ProductConflict, match="changed after retry validation"):
        store.retry_execution(
            source.execution_id,
            request_id="same-retry-intent",
            expected_revision=source.revision,
            expected_space_revision=original.revision,
        )
    assert store.list_executions().total == 1
    assert store.messages().total == 0
    created = store.retry_execution(
        source.execution_id,
        request_id="same-retry-intent",
        expected_revision=source.revision,
        expected_space_revision=changed.revision,
    )
    assert created.config_snapshot == changed
    assert created.source_execution_id == source.execution_id
    assert created.parent_execution_id is None and created.session_id != source.session_id
    assert store.get_execution(source.execution_id) == source
    assert store.messages().total == 1
    assert (
        store.retry_execution(
            source.execution_id,
            request_id="same-retry-intent",
            expected_revision=source.revision,
            expected_space_revision=original.revision,
        )
        == created
    )
