from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pleiad.agent.agent import Agent
from pleiad.context.selection.models import ContextBlock
from pleiad.orchestration.collaboration.contracts import WorkPermit, WorkProposal, WorkRejection
from pleiad.runtime.execution.control import RuntimeHandoffBinding
from pleiad.runtime.runner import Runner

if TYPE_CHECKING:
    from pleiad.orchestration.collaboration.endpoint import MemberEndpoint


@dataclass(frozen=True, slots=True)
class WorkBinding:
    """Borrow host-owned execution resources for an authorized member invocation."""

    agent: Agent
    runner: Runner
    binding_key: str
    context: tuple[ContextBlock, ...] = ()
    handoffs: tuple[RuntimeHandoffBinding, ...] = ()


@dataclass(frozen=True, slots=True)
class WorkAdmission:
    """Return a durable permit together with its concrete current execution binding."""

    permit: WorkPermit
    binding: WorkBinding


@dataclass(frozen=True, slots=True)
class CollaborationHost:
    """Bind actual product admission and current authorization at two execution boundaries.

    Callbacks must be idempotent by work ID, must not reenter the group, and must
    bind at least one endpoint Tool to every child Agent. Shared resources remain
    host-owned. Product Execution records belong in these callbacks, not the SDK.
    """

    admit_work: Callable[[WorkProposal, MemberEndpoint], Awaitable[WorkAdmission | WorkRejection]]
    rebind_work: Callable[[WorkProposal, WorkPermit, MemberEndpoint], Awaitable[WorkBinding | WorkRejection]]
