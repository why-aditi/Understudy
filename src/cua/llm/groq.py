"""Groq free-tier client: text-only auxiliary passes that preserve Gemini quota."""

import json
import os
from typing import Any

import httpx

from cua.llm.base import LLMResponse, Message, ToolCall, ToolSpec
from cua.llm.limiter import LLMError, RateLimiter

DEFAULT_MODEL = "openai/gpt-oss-120b"
DEFAULT_BASE_URL = "https://api.groq.com/openai/v1"


class GroqClient:
    """OpenAI-compatible chat completions. Text only - the free vision models are not usable."""

    supports_images = False

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        client: httpx.Client | None = None,
        limiter: RateLimiter | None = None,
        rpm: int = 30,
        timeout: float = 60.0,
    ) -> None:
        key = api_key or os.environ.get("GROQ_API_KEY")
        if not key:
            raise LLMError("GROQ_API_KEY is not set (copy .env.example to .env)")
        self.api_key = key
        self.model = model or os.environ.get("GROQ_MODEL") or DEFAULT_MODEL
        self.base_url = base_url.rstrip("/")
        self.limiter = limiter or RateLimiter(rpm)
        self._client = client or httpx.Client(timeout=timeout)

    def complete(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        images: list[bytes] | None = None,
    ) -> LLMResponse:
        if images:
            raise LLMError("groq: this client is text-only; send images to GeminiClient")

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": 0,
        }
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in tools
            ]

        response = self.limiter.call(
            lambda: self._client.post(
                f"{self.base_url}/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {self.api_key}"},
            ),
            provider="groq",
        )
        if response.status_code >= 400:
            raise LLMError(f"groq: HTTP {response.status_code}: {response.text.strip()[:300]}")
        return self._parse(response)

    def _parse(self, response: httpx.Response) -> LLMResponse:
        try:
            data: dict[str, Any] = response.json()
        except ValueError as exc:
            raise LLMError(f"groq: response was not JSON: {response.text[:200]}") from exc

        choices: list[dict[str, Any]] = data.get("choices") or []
        if not choices:
            raise LLMError(f"groq: no choices in response: {str(data)[:300]}")

        message: dict[str, Any] = choices[0].get("message") or {}
        calls = [
            ToolCall(name=c["function"]["name"], arguments=_args(c))
            for c in message.get("tool_calls") or []
        ]
        return LLMResponse(
            model=data.get("model") or self.model,
            # Reasoning models drop `content` entirely when they emit a tool call and put
            # the rationale in `reasoning`. Without this the evidence has a blank column.
            text=message.get("content") or message.get("reasoning"),
            tool_calls=calls,
            finish_reason=choices[0].get("finish_reason"),
        )


def _args(call: dict[str, Any]) -> dict[str, Any]:
    """OpenAI-shaped tool arguments arrive as a JSON string."""
    raw = call["function"].get("arguments") or "{}"
    try:
        parsed: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LLMError(f"groq: tool arguments were not JSON: {raw[:200]}") from exc
    if not isinstance(parsed, dict):
        raise LLMError(f"groq: tool arguments were not an object: {raw[:200]}")
    return parsed
