from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Unpack

from fastmcp.client.transports import ClientTransport, StdioTransport
from fastmcp.client.transports.base import SessionKwargs, TransportOptions

from mcp import ClientSession


@dataclass(frozen=True, slots=True)
class IsolatedStdioTransport(ClientTransport):
    """Give each SDK client session its own subprocess and cleanup lifetime.

    FastMCP client clones share their transport. A shared stdio transport would
    let one completed or cancelled call disconnect a concurrent sibling call.
    """

    command: str
    args: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        """Snapshot process settings before clients begin concurrent sessions."""
        object.__setattr__(self, "env", MappingProxyType(dict(self.env)))

    @asynccontextmanager
    async def connect_session(
        self,
        *,
        transport_options: TransportOptions | None = None,
        **session_kwargs: Unpack[SessionKwargs],
    ) -> AsyncGenerator[ClientSession, None]:
        """Start and close one subprocess within the requesting client's lifetime."""
        transport = StdioTransport(self.command, list(self.args), env=dict(self.env), keep_alive=False)
        async with transport.connect_session(transport_options=transport_options, **session_kwargs) as session:
            yield session
