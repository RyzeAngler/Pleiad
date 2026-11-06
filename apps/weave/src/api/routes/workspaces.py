import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request

from pleiad_weave.agent.contracts import GraphSpec
from pleiad_weave.api.dependencies import get_service
from pleiad_weave.application.service import ExecutionService, wait_owned_operation
from pleiad_weave.application.space_drafts import (
    SpaceDraftError,
    SpaceDraftRequest,
    SpaceDraftResponse,
    SpaceDraftTimeout,
    draft_space,
    validate_resource_preferences,
)
from pleiad_weave.application.workspaces import (
    CapabilityResource,
    CreateConversationRequest,
    DeleteWorkspaceResponse,
    ModelResource,
    ResourceResponse,
    UpdateSpaceRequest,
    create_conversation,
    create_space,
    validate_saved_graph,
)
from pleiad_weave.infrastructure.storage.workspaces import (
    ConversationRecord,
    SpaceConfiguration,
    SpaceDefinition,
    SpaceRecord,
    WorkspaceConflictError,
)

router = APIRouter(prefix="/v1")
service_dependency = Depends(get_service)


@router.get("/spaces", response_model=list[SpaceRecord])
async def list_spaces(service: ExecutionService = service_dependency) -> list[SpaceRecord]:
    """List the current owner's reusable spaces."""
    return list(await asyncio.to_thread(service.workspaces.list_spaces))


@router.post("/spaces", response_model=SpaceRecord, status_code=201)
async def add_space(request: SpaceDefinition, service: ExecutionService = service_dependency) -> SpaceRecord:
    """Persist a reusable space without submitting a model request."""
    try:
        validate_resource_preferences(request, await service.weave.capabilities())
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return await asyncio.to_thread(create_space, service.workspaces, request)


@router.post("/spaces/draft", response_model=SpaceDraftResponse)
async def propose_space(
    request: SpaceDraftRequest,
    connection: Request,
    service: ExecutionService = service_dependency,
) -> SpaceDraftResponse:
    """Draft settings without persistence and cancel the Run on disconnection."""
    owner = asyncio.current_task()
    if owner is None:
        raise RuntimeError("A draft request requires an owning task")

    async def watch_disconnect() -> None:
        """Cancel the request-owned SDK invocation when its client leaves."""
        while True:
            message = await connection.receive()
            if message["type"] == "http.disconnect":
                owner.cancel()
                return

    failure: SpaceDraftError | None = None
    response: SpaceDraftResponse | None = None
    async with asyncio.TaskGroup() as tasks:
        watcher = tasks.create_task(watch_disconnect())
        try:
            response = await draft_space(service.weave, request)
        except SpaceDraftError as error:
            failure = error
        finally:
            watcher.cancel()
    if failure is not None:
        code = 504 if isinstance(failure, SpaceDraftTimeout) else 502
        raise HTTPException(status_code=code, detail=str(failure)) from failure
    if response is None:
        raise RuntimeError("A draft request completed without a response")
    return response


@router.patch("/spaces/{space_id}", response_model=SpaceRecord)
async def update_space(
    space_id: str, request: UpdateSpaceRequest, service: ExecutionService = service_dependency
) -> SpaceRecord:
    """Update space settings without changing already submitted Executions."""
    configuration = SpaceConfiguration.model_validate(request.model_dump(exclude={"expected_revision"}))
    try:
        validate_resource_preferences(configuration, await service.weave.capabilities())
        async with service.workspace_mutation_lock:
            await wait_owned_operation(
                asyncio.create_task(
                    asyncio.to_thread(
                        validate_saved_graph,
                        service.workspaces,
                        service.repository,
                        service.weave.graph_store,
                        space_id,
                        configuration.saved_graph,
                    )
                )
            )
            return await wait_owned_operation(
                asyncio.create_task(
                    asyncio.to_thread(
                        service.workspaces.update_space, space_id, configuration, request.expected_revision
                    )
                )
            )
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except WorkspaceConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.delete("/spaces/{space_id}", response_model=DeleteWorkspaceResponse)
async def delete_space(space_id: str, service: ExecutionService = service_dependency) -> DeleteWorkspaceResponse:
    """Delete a space and all stopped tasks that belong to it."""
    try:
        await service.delete_workspace(space_id=space_id)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except WorkspaceConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return DeleteWorkspaceResponse()


@router.delete("/conversations/{conversation_id}", response_model=DeleteWorkspaceResponse)
async def delete_conversation(
    conversation_id: str, service: ExecutionService = service_dependency
) -> DeleteWorkspaceResponse:
    """Delete one stopped task while retaining its containing space."""
    try:
        await service.delete_workspace(conversation_id=conversation_id)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except WorkspaceConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return DeleteWorkspaceResponse()


@router.get("/spaces/{space_id}/conversations", response_model=list[ConversationRecord])
async def list_conversations(space_id: str, service: ExecutionService = service_dependency) -> list[ConversationRecord]:
    """List only conversations belonging to the selected space."""
    try:
        return list(await asyncio.to_thread(service.workspaces.list_conversations, space_id))
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.get("/spaces/{space_id}/saved-graph", response_model=GraphSpec)
async def saved_graph(space_id: str, service: ExecutionService = service_dependency) -> GraphSpec:
    """Read the exact Graph revision referenced by a space without executing it.

    Args:
        space_id: Space whose saved orchestration should be inspected.
        service: Owner of workspace records and immutable Graph revisions.

    Returns:
        The saved Graph definition.

    Raises:
        HTTPException: If the space or its referenced Graph cannot be found.
    """
    space = await asyncio.to_thread(service.workspaces.get_space, space_id)
    if space is None:
        raise HTTPException(status_code=404, detail="Space not found")
    if space.saved_graph is None:
        raise HTTPException(status_code=404, detail="No Graph saved in this space")
    revision = await asyncio.to_thread(
        service.weave.graph_store.revision, space.saved_graph.graph_id, space.saved_graph.revision
    )
    if revision is None:
        raise HTTPException(status_code=404, detail="Saved Graph revision not found")
    return revision.graph


@router.post("/spaces/{space_id}/conversations", response_model=ConversationRecord, status_code=201)
async def add_conversation(
    space_id: str, request: CreateConversationRequest, service: ExecutionService = service_dependency
) -> ConversationRecord:
    """Create an independent task conversation in the selected space."""
    if await asyncio.to_thread(service.workspaces.get_space, space_id) is None:
        raise HTTPException(status_code=404, detail="Space not found")
    try:
        async with service.workspace_mutation_lock:
            return await wait_owned_operation(
                asyncio.create_task(asyncio.to_thread(create_conversation, service.workspaces, space_id, request))
            )
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.get("/resources", response_model=ResourceResponse)
async def resources(service: ExecutionService = service_dependency) -> ResourceResponse:
    """Read actual SDK resource metadata without serializing credentials."""
    snapshot = await service.weave.capabilities()
    return ResourceResponse(
        models=[ModelResource(role=role, model=model.model) for role, model in service.weave.models.items()],
        tools=[
            CapabilityResource(code=tool.code, name=tool.name, description=tool.description)
            for tool in snapshot.tools.descriptors.values()
        ],
        skills=[
            CapabilityResource(code=skill.code, name=skill.name, description=skill.description)
            for skill, package in snapshot.skills.bindings.values()
        ],
        workspace_path=str(service.weave.workspace),
        capability_file=str(service.weave.manifest),
    )
