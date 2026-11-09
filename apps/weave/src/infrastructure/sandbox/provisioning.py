import os

from pleiad.sandbox.policy import SandboxPolicy


def default_policy(image: str) -> SandboxPolicy:
    """Bind the product's pinned image to the SDK's strict OCI policy."""
    return SandboxPolicy(image=image, user=f"{os.getuid()}:{os.getgid()}", allow_shell_command=True)
