from pleiad.sandbox.backends.oci import OciSandbox
from pleiad.sandbox.errors import SandboxConfigurationError
from pleiad.sandbox.models import SandboxRequest, SandboxResult


class Sandbox:
    """Validate a resolved request and delegate it to one selected OCI backend."""

    def __init__(self, backend: OciSandbox) -> None:
        """Bind the host-selected OCI backend without a local fallback."""
        self.backend = backend

    async def execute(self, request: SandboxRequest) -> SandboxResult:
        """Execute one request without adding a second execution timeout."""
        invalid_environment = set(request.environment) - request.policy.allowed_environment
        if invalid_environment:
            raise SandboxConfigurationError("Sandbox request contains an environment name outside its policy.")
        return await self.backend.execute(request)
