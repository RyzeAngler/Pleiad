from datetime import UTC, datetime

from pleiad.runtime.policy import EffectivePolicy
from pleiad.tools.contracts import ToolContext


def child_policy(context: ToolContext) -> EffectivePolicy | None:
    """Intersect nested execution with the parent deadline, grants, and remaining allowance."""
    limits = context.parent_limits
    if limits is None:
        return None
    return EffectivePolicy(
        timeout_seconds=max(0.0, (limits.deadline - datetime.now(UTC)).total_seconds()),
        max_model_steps=limits.remaining_model_steps,
        max_output_tokens=limits.remaining_output_tokens,
        tool_concurrency=limits.tool_concurrency,
        grants=context.grants,
        deadline=limits.deadline,
    )
