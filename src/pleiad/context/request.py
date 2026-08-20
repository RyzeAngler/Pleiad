"""Assemble complete provider-neutral model requests."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

from pleiad.llm.contracts import (
    LLMRequest,
    Message,
    ResponseFormat,
    ResponseFormatType,
    ThinkingConfig,
    ToolDefinition,
)

if TYPE_CHECKING:
    from pleiad.agent.contracts import AgentDefinition


class RequestAssembler:
    """Build a complete LLM request from already-selected Runtime inputs."""

    def assemble(
        self,
        definition: AgentDefinition,
        messages: tuple[Message, ...],
        tool_definitions: tuple[ToolDefinition, ...],
        *,
        system_prompt: str,
    ) -> LLMRequest:
        """Combine instructions, messages, tools, and output policy."""
        return LLMRequest(
            request_id=uuid4().hex,
            model=definition.client.model,
            messages=messages,
            tools=tool_definitions,
            tool_choice="auto" if tool_definitions else "none",
            response_format=self._response_format(definition),
            thinking=ThinkingConfig(
                enabled=definition.client.capabilities.thinking,
                effort="medium",
            ),
            max_output_tokens=definition.policy.max_output_tokens,
            stream=True,
            metadata={"system_prompt_length": len(system_prompt)},
        )

    @staticmethod
    def _response_format(definition: AgentDefinition) -> ResponseFormat:
        """Use the strongest structured response format exposed by the Model."""
        schema = definition.output_schema
        if schema is None:
            return ResponseFormat()
        capabilities = definition.client.capabilities
        if ResponseFormatType.JSON_SCHEMA in capabilities.response_formats:
            return ResponseFormat(
                type=ResponseFormatType.JSON_SCHEMA,
                name="node_output",
                json_schema=schema,
                strict=capabilities.strict_json_schema,
            )
        return ResponseFormat(type=ResponseFormatType.JSON_OBJECT)
