"""LLMClient protocol: the provider-agnostic completion interface."""

from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field

Role = Literal["system", "user", "assistant", "tool"]


class Message(BaseModel):
    """One turn of a conversation sent to a provider."""

    role: Role
    content: str


class ToolSpec(BaseModel):
    """A tool the model may call, described as JSON Schema."""

    name: str
    description: str
    parameters: dict[str, Any]


class ToolCall(BaseModel):
    """A single tool invocation emitted by the model."""

    name: str
    arguments: dict[str, Any]


class LLMResponse(BaseModel):
    """Normalised provider response: free text, tool calls, or both."""

    model: str
    text: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    finish_reason: str | None = None


@runtime_checkable
class LLMClient(Protocol):
    """Every provider client implements exactly this."""

    # Whether this provider accepts images. Capturing a screenshot for evidence and
    # sending one to a model are separate decisions; this settles only the second.
    supports_images: bool

    def complete(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        images: list[bytes] | None = None,
    ) -> LLMResponse:
        """Return one completion for `messages`, optionally with tools and images."""
        ...
