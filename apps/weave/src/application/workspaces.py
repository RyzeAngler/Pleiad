from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from pleiad.contracts import ContractModel, JsonObject
from pydantic import Field, field_validator

from pleiad_weave.infrastructure.storage.graphs import TaskGraphStore
from pleiad_weave.infrastructure.storage.repository import ExecutionRepository
from pleiad_weave.infrastructure.storage.workspaces import (
    ConversationRecord,
    SavedGraphReference,
    SpaceConfiguration,
    SpaceDefinition,
    SpaceRecord,
    WorkspaceRepository,
)


class UpdateSpaceRequest(SpaceConfiguration):
    """Replace one observed revision of a reusable space configuration."""

    expected_revision: int = Field(ge=1)


class DeleteWorkspaceResponse(ContractModel):
    """Confirm that a space or conversation and its owned history were deleted."""

    deleted: Literal[True] = True


class CreateConversationRequest(ContractModel):
    """Name an independent conversation inside a product space."""

    title: str = Field(min_length=1, max_length=160)

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        """Reject a blank task name and normalize its outer whitespace."""
        value = value.strip()
        if not value:
            raise ValueError("Conversation title must not be blank")
        return value


class ModelResource(ContractModel):
    """Expose one configured model role without credentials or client internals."""

    role: str
    model: str


class CapabilityResource(ContractModel):
    """Expose the shared public metadata of a Tool or Skill."""

    code: str
    name: str
    description: str


class ResourceResponse(ContractModel):
    """Describe the actual local capability catalog without secret settings."""

    models: list[ModelResource]
    tools: list[CapabilityResource]
    skills: list[CapabilityResource]
    workspace_path: str
    capability_file: str


def create_space(repository: WorkspaceRepository, request: SpaceDefinition) -> SpaceRecord:
    """Generate the identity and timestamps of one reusable product space."""
    now = datetime.now(UTC)
    return repository.create_space(
        SpaceRecord(**request.model_dump(), space_id=uuid4().hex, created_at=now, updated_at=now)
    )


def create_conversation(
    repository: WorkspaceRepository, space_id: str, request: CreateConversationRequest
) -> ConversationRecord:
    """Create a separate conversation while retaining shared space settings."""
    now = datetime.now(UTC)
    return repository.create_conversation(
        ConversationRecord(
            conversation_id=uuid4().hex, space_id=space_id, title=request.title, created_at=now, updated_at=now
        )
    )


def validate_saved_graph(
    repository: WorkspaceRepository,
    executions: ExecutionRepository,
    graphs: TaskGraphStore,
    space_id: str,
    reference: SavedGraphReference | None,
) -> None:
    """Require a stored Graph revision and durable evidence from this space.

    A capability preference is not an authorization boundary. This check proves
    the saved orchestration actually occurred in one of the space's tasks.

    Raises:
        LookupError: If the space does not exist.
        ValueError: If the Graph revision is missing or belongs to no task here.
    """
    if reference is None:
        return
    conversations = repository.list_conversations(space_id)
    if graphs.revision(reference.graph_id, reference.revision) is None:
        raise ValueError("Saved Graph revision does not exist")
    space = repository.get_space(space_id)
    if space is not None and space.saved_graph == reference:
        return
    for conversation in conversations:
        for execution in executions.list_records(conversation.conversation_id):
            if (execution.graph_id, execution.graph_revision) == (reference.graph_id, reference.revision):
                return
            for event in executions.read_events(execution.execution_id):
                if (
                    event.event_type == "graph.revision.accepted"
                    and event.data.get("graph_id") == reference.graph_id
                    and event.data.get("revision") == reference.revision
                ):
                    return
    raise ValueError("Saved Graph revision must come from an Execution in this space")


def execution_input(repository: WorkspaceRepository, conversation_id: str, user_input: JsonObject) -> JsonObject:
    """Attach server-owned space context while preserving the submitted objective.

    Preferences guide orchestration; they do not grant or restrict SDK permissions.
    The persisted Execution input retains this revision when the space changes.
    """
    space = repository.execution_space(conversation_id)
    if space is None:
        return user_input
    context: JsonObject = {
        "space_id": space.space_id,
        "name": space.name,
        "preferred_tool_codes": list(space.preferred_tool_codes),
        "preferred_skill_codes": list(space.preferred_skill_codes),
        "revision": space.revision,
        "saved_graph": (
            None
            if space.saved_graph is None
            else {"graph_id": space.saved_graph.graph_id, "revision": space.saved_graph.revision}
        ),
    }
    if space.instructions.strip():
        context["instructions"] = space.instructions
    if space.saved_graph is not None:
        context["saved_graph_usage"] = (
            "This space retains an existing Graph for reuse in this independent task. "
            "Use inspect_graph with its graph_id and graph_revision set to saved_graph.revision to review it. "
            "When appropriate for the user's request, use execute_graph with that same graph_id, "
            "graph_revision, and the new task input. Do not copy or resubmit the Graph merely to reuse it. "
            "The saved reference is context, not permission to execute without required approvals."
        )
    return {**user_input, "weave_space": context}
