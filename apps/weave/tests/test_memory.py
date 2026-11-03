import multiprocessing
from multiprocessing.connection import Connection
from multiprocessing.synchronize import Barrier
from pathlib import Path

import pytest
from pydantic import TypeAdapter
from typer.testing import CliRunner

from pleiad_weave.infrastructure.storage.memory import MemoryConflictError, MemoryFact, MemoryStore
from pleiad_weave.memory_cli import memory_app


@pytest.fixture
def memory(tmp_path: Path) -> MemoryStore:
    store = MemoryStore(tmp_path / "workspace" / "runtime" / "memory", user_id="alice")
    store.save_fact(MemoryFact(memory_id="preference", scope="conversation", scope_id="one", content="favorite tea"))
    return store


def update_in_process(root: Path, barrier: Barrier, output: Connection) -> None:
    store = MemoryStore(root, user_id="alice")
    barrier.wait(timeout=10.0)
    try:
        fact = store.update_fact("conversation", "preference", "new tea", expected_revision=1, conversation_id="one")
    except MemoryConflictError as error:
        output.send(error.code)
    else:
        output.send(str(fact.revision))
    finally:
        output.close()


def test_memory_edits_persist_host_provenance_without_inventing_old_sources(memory: MemoryStore) -> None:
    updated = memory.update_fact(
        "conversation", "preference", "favorite coffee", expected_revision=1, conversation_id="one"
    )

    reopened = MemoryStore(memory.root, user_id="alice")
    history = reopened.fact_history("conversation", "preference", conversation_id="one")
    assert [fact.revision for fact in history] == [1, 2]
    assert history[0].source is None
    assert history[0].recorded_at is None
    assert updated == history[1]
    assert updated.source == "local_user"
    assert updated.recorded_at is not None
    offset = updated.recorded_at.utcoffset()
    assert offset is not None
    assert offset.total_seconds() == 0
    assert reopened.recall("one", "coffee", 5) == (updated,)


def test_deactivated_memory_stays_visible_in_history_but_is_not_recalled(memory: MemoryStore) -> None:
    stopped = memory.deactivate_fact("conversation", "preference", expected_revision=1, conversation_id="one")

    assert stopped.revision == 2
    assert not stopped.active
    assert stopped.content == "favorite tea"
    assert stopped.source == "local_user"
    assert memory.recall("one", "tea", 5) == ()
    assert memory.list_facts("conversation", conversation_id="one") == ()
    assert memory.list_facts("conversation", conversation_id="one", include_inactive=True) == (stopped,)
    assert [fact.active for fact in memory.fact_history("conversation", "preference", conversation_id="one")] == [
        True,
        False,
    ]


def test_stale_memory_edit_does_not_append_another_revision(memory: MemoryStore) -> None:
    memory.update_fact("conversation", "preference", "new tea", expected_revision=1, conversation_id="one")

    with pytest.raises(MemoryConflictError) as caught:
        memory.deactivate_fact("conversation", "preference", expected_revision=1, conversation_id="one")

    assert caught.value.code == "weave.memory_conflict"
    assert "current revision is 2" in caught.value.message
    assert [fact.revision for fact in memory.fact_history("conversation", "preference", conversation_id="one")] == [
        1,
        2,
    ]
    assert memory.list_facts("conversation", conversation_id="one")[0].active


@pytest.mark.parametrize("revision", [0, -1])
def test_memory_edits_require_a_positive_expected_revision(memory: MemoryStore, revision: int) -> None:
    with pytest.raises(ValueError, match="expected_revision must be positive"):
        memory.update_fact("conversation", "preference", "new tea", expected_revision=revision, conversation_id="one")

    assert len(memory.fact_history("conversation", "preference", conversation_id="one")) == 1


def test_save_fact_cannot_bypass_revision_checked_memory_edits(memory: MemoryStore) -> None:
    with pytest.raises(ValueError, match="updates require expected_revision"):
        memory.save_fact(
            MemoryFact(memory_id="preference", scope="conversation", scope_id="one", content="forged", revision=99)
        )
    with pytest.raises(MemoryConflictError):
        memory.save_fact(MemoryFact(memory_id="preference", scope="conversation", scope_id="one", content="duplicate"))
    with pytest.raises(ValueError, match="start at revision 1"):
        memory.save_fact(MemoryFact(memory_id="new", scope="conversation", scope_id="one", content="gap", revision=7))

    facts = memory.list_facts("conversation", conversation_id="one")
    assert len(facts) == 1
    assert facts[0].content == "favorite tea"
    assert facts[0].revision == 1


def test_missing_memory_update_does_not_create_a_fact(memory: MemoryStore) -> None:
    with pytest.raises(LookupError, match="not found"):
        memory.update_fact("conversation", "missing", "new tea", expected_revision=1, conversation_id="one")

    assert memory.fact_history("conversation", "missing", conversation_id="one") == ()


def test_empty_memory_update_does_not_append_invalid_content(memory: MemoryStore) -> None:
    with pytest.raises(ValueError):
        memory.update_fact("conversation", "preference", "", expected_revision=1, conversation_id="one")

    assert memory.list_facts("conversation", conversation_id="one")[0].content == "favorite tea"
    assert len(memory.fact_history("conversation", "preference", conversation_id="one")) == 1


def test_legacy_memory_records_keep_unknown_provenance(memory: MemoryStore) -> None:
    original = memory.list_facts("conversation", conversation_id="one")[0]
    memory.fact_path("conversation", "one").write_text(
        original.model_dump_json(exclude_none=True) + "\n", encoding="utf-8"
    )

    restored = MemoryStore(memory.root, user_id="alice").recall("one", "tea", 5)[0]

    assert restored.content == "favorite tea"
    assert restored.source is None
    assert restored.recorded_at is None


def test_memory_management_and_recall_stay_in_the_selected_workspace_and_conversation(
    memory: MemoryStore, tmp_path: Path
) -> None:
    other_project = MemoryStore(tmp_path / "other-workspace" / "runtime" / "memory", user_id="alice")

    assert other_project.list_facts("conversation", conversation_id="one") == ()
    assert memory.list_facts("conversation", conversation_id="two") == ()
    assert memory.recall("two", "tea", 5) == ()
    with pytest.raises(ValueError, match="conversation_id is required"):
        memory.list_facts("conversation")
    with pytest.raises(PermissionError):
        memory.list_facts("user")


def test_user_memory_only_uses_the_opted_in_deployment_user(memory: MemoryStore) -> None:
    authorized = MemoryStore(memory.root, user_id="alice", cross_conversation=True)
    authorized.save_fact(MemoryFact(memory_id="user-fact", scope="user", scope_id="alice", content="favorite tea"))

    assert authorized.recall("two", "tea", 5)[0].memory_id == "user-fact"
    assert authorized.list_facts("user")[0].scope_id == "alice"
    assert MemoryStore(memory.root, user_id="bob", cross_conversation=True).list_facts("user") == ()
    with pytest.raises(PermissionError):
        authorized.save_fact(MemoryFact(memory_id="foreign", scope="user", scope_id="bob", content="private"))
    with pytest.raises(ValueError, match="only valid for conversation"):
        authorized.list_facts("user", conversation_id="one")


@pytest.mark.parametrize("corruption", ["scope", "revision"])
def test_memory_recall_rejects_mismatched_or_broken_revision_journals(memory: MemoryStore, corruption: str) -> None:
    record = MemoryFact(
        memory_id="preference",
        scope="conversation",
        scope_id="other" if corruption == "scope" else "one",
        content="injected",
        revision=2 if corruption == "scope" else 3,
    )
    with memory.fact_path("conversation", "one").open("a", encoding="utf-8") as stream:
        stream.write(record.model_dump_json() + "\n")

    with pytest.raises(ValueError, match=r"scope does not match|invalid revision chain"):
        memory.recall("one", "tea", 5)


def test_two_memory_management_processes_cannot_both_update_the_same_revision(memory: MemoryStore) -> None:
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    pipes = [context.Pipe(duplex=False), context.Pipe(duplex=False)]
    processes = [
        context.Process(target=update_in_process, args=(memory.root, barrier, send)) for receive, send in pipes
    ]
    try:
        for process in processes:
            process.start()
        outcomes: list[str] = []
        for receive, send in pipes:
            send.close()
            assert receive.poll(10.0), "memory management process did not finish"
            value: object = receive.recv()
            assert isinstance(value, str)
            outcomes.append(value)
        assert sorted(outcomes) == ["2", "weave.memory_conflict"]
        for process in processes:
            process.join(timeout=10.0)
            assert process.exitcode == 0
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
            if process.pid is not None:
                process.join(timeout=10.0)
        for receive, send in pipes:
            receive.close()
            send.close()

    assert [fact.revision for fact in memory.fact_history("conversation", "preference", conversation_id="one")] == [
        1,
        2,
    ]


def test_memory_cli_lists_views_edits_and_deactivates_without_model_or_sandbox_configuration(
    memory: MemoryStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PLEIAD_SANDBOX_IMAGE", raising=False)
    monkeypatch.delenv("PLEIAD_LLM_API_KEY", raising=False)
    arguments = ["--workspace", str(tmp_path / "workspace"), "--conversation-id", "one"]
    runner = CliRunner()
    decoder = TypeAdapter(tuple[MemoryFact, ...])

    listed = runner.invoke(memory_app, [*arguments, "list"])
    assert listed.exit_code == 0, listed.output
    assert decoder.validate_json(listed.stdout)[0].content == "favorite tea"
    edited = runner.invoke(
        memory_app, [*arguments, "update", "preference", "--content", "favorite coffee", "--expected-revision", "1"]
    )
    assert edited.exit_code == 0, edited.output
    assert decoder.validate_json(edited.stdout)[0].source == "local_user"
    shown = runner.invoke(memory_app, [*arguments, "show", "preference", "--history"])
    assert shown.exit_code == 0, shown.output
    assert [fact.revision for fact in decoder.validate_json(shown.stdout)] == [1, 2]
    stopped = runner.invoke(memory_app, [*arguments, "deactivate", "preference", "--expected-revision", "2"])
    assert stopped.exit_code == 0, stopped.output
    assert not decoder.validate_json(stopped.stdout)[0].active
    assert memory.recall("one", "coffee", 5) == ()
    all_facts = runner.invoke(memory_app, [*arguments, "list", "--all"])
    assert all_facts.exit_code == 0, all_facts.output
    assert decoder.validate_json(all_facts.stdout)[0].revision == 3


def test_memory_cli_reports_version_conflicts_without_overwriting_current_content(
    memory: MemoryStore, tmp_path: Path
) -> None:
    memory.update_fact("conversation", "preference", "new tea", expected_revision=1, conversation_id="one")
    result = CliRunner().invoke(
        memory_app,
        [
            "--workspace",
            str(tmp_path / "workspace"),
            "--conversation-id",
            "one",
            "update",
            "preference",
            "--content",
            "stale edit",
            "--expected-revision",
            "1",
        ],
    )

    assert result.exit_code == 2
    assert "weave.memory_conflict" in result.stderr
    assert memory.list_facts("conversation", conversation_id="one")[0].content == "new tea"


def test_memory_cli_user_scope_only_selects_the_deployment_configured_identity(
    memory: MemoryStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for user_id in ("alice", "bob"):
        store = MemoryStore(memory.root, user_id=user_id, cross_conversation=True)
        store.save_fact(MemoryFact(memory_id="preference", scope="user", scope_id=user_id, content=f"{user_id} tea"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PLEIAD_MEMORY_USER_ID", "alice")
    monkeypatch.setenv("PLEIAD_MEMORY_CROSS_CONVERSATION", "true")

    result = CliRunner().invoke(memory_app, ["--workspace", str(tmp_path / "workspace"), "--scope", "user", "list"])

    assert result.exit_code == 0, result.output
    facts = TypeAdapter(tuple[MemoryFact, ...]).validate_json(result.stdout)
    assert len(facts) == 1
    assert facts[0].scope_id == "alice"
    assert facts[0].content == "alice tea"


@pytest.mark.parametrize("arguments", [["list"], ["--scope", "user", "list"]])
def test_memory_cli_requires_an_explicit_authorized_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arguments: list[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PLEIAD_MEMORY_CROSS_CONVERSATION", "false")
    result = CliRunner().invoke(memory_app, ["--workspace", str(tmp_path / "workspace"), *arguments])

    assert result.exit_code == 2
    assert "required" in result.stderr or "authorization scope" in result.stderr
