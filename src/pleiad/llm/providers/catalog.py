"""Built-in provider profile catalog."""

from pleiad.llm.providers.anthropic import profile as anthropic_profile
from pleiad.llm.providers.deepseek import profile as deepseek_profile
from pleiad.llm.providers.glm import profile as glm_profile
from pleiad.llm.providers.kimi import profile as kimi_profile
from pleiad.llm.providers.minimax import anthropic_profile as minimax_anthropic_profile
from pleiad.llm.providers.minimax import profile as minimax_profile
from pleiad.llm.providers.openai import profile as openai_profile
from pleiad.llm.providers.qwen import profile as qwen_profile
from pleiad.llm.registry import ProviderProfile


def builtin_profiles() -> tuple[ProviderProfile, ...]:
    """Return all built-in protocol/provider profiles."""
    return (
        anthropic_profile(),
        deepseek_profile(),
        glm_profile(),
        kimi_profile(),
        minimax_profile(),
        minimax_anthropic_profile(),
        openai_profile(),
        qwen_profile(),
    )
