from pleiad.limits import seconds_remaining
from pleiad.runtime.policy import EffectivePolicy
from pleiad.tools.contracts import ToolContext


def child_policy(context: ToolContext) -> EffectivePolicy | None:
    """Intersect nested execution with the parent deadline, grants, and remaining allowance."""
    limits = context.parent_limits
    if limits is None:
        return None
    return EffectivePolicy(
        timeout_seconds=seconds_remaining(limits.deadline),
        max_model_steps=limits.remaining_model_steps,
        max_output_tokens=limits.remaining_output_tokens,
        max_response_output_tokens=limits.max_response_output_tokens,
        tool_concurrency=limits.tool_concurrency,
        grants=context.grants,
        deadline=limits.deadline,
    )
