"""Gemini free-tier client: the primary vision plus tool-calling provider for discovery."""

import base64
import os
from typing import Any

import httpx

from cua.llm.base import LLMResponse, Message, ToolCall, ToolSpec
from cua.llm.limiter import LLMError, RateLimiter

DEFAULT_MODEL = "gemini-2.5-flash"
DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"

# Gemini names the assistant role "model" and carries system text out of band.
_ROLE_MAP = {"user": "user", "assistant": "model", "tool": "user"}


class GeminiClient:
    """Calls generateContent on a Flash model. Accepts images and function declarations."""

    supports_images = True

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        client: httpx.Client | None = None,
        limiter: RateLimiter | None = None,
        timeout: float = 60.0,
    ) -> None:
        key = api_key or os.environ.get("GEMINI_API_KEY")
        if not key:
            raise LLMError("GEMINI_API_KEY is not set (copy .env.example to .env)")
        self.api_key = key
        self.model = model or os.environ.get("GEMINI_MODEL") or DEFAULT_MODEL
        self.base_url = base_url.rstrip("/")
        self.limiter = limiter or RateLimiter()
        self._client = client or httpx.Client(timeout=timeout)

    def complete(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        images: list[bytes] | None = None,
    ) -> LLMResponse:
        payload = self._build_payload(messages, tools, images)
        url = f"{self.base_url}/models/{self.model}:generateContent"
        response = self.limiter.call(
            lambda: self._client.post(url, json=payload, headers={"x-goog-api-key": self.api_key}),
            provider="gemini",
        )
        if response.status_code >= 400:
            raise LLMError(f"gemini: HTTP {response.status_code}: {response.text.strip()[:300]}")
        return self._parse(response)

    def _build_payload(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None,
        images: list[bytes] | None,
    ) -> dict[str, Any]:
        system = [m.content for m in messages if m.role == "system"]
        contents: list[dict[str, Any]] = [
            {"role": _ROLE_MAP[m.role], "parts": [{"text": m.content}]}
            for m in messages
            if m.role != "system"
        ]
        if images:
            if not contents:
                contents.append({"role": "user", "parts": []})
            # Screenshots are a secondary channel, attached to the newest turn.
            parts: list[dict[str, Any]] = contents[-1]["parts"]
            parts.extend(
                {
                    "inlineData": {
                        "mimeType": "image/png",
                        "data": base64.b64encode(image).decode("ascii"),
                    }
                }
                for image in images
            )

        payload: dict[str, Any] = {"contents": contents, "generationConfig": {"temperature": 0}}
        if system:
            payload["systemInstruction"] = {"parts": [{"text": "\n\n".join(system)}]}
        if tools:
            payload["tools"] = [
                {
                    "functionDeclarations": [
                        {"name": t.name, "description": t.description, "parameters": t.parameters}
                        for t in tools
                    ]
                }
            ]
        return payload

    def _parse(self, response: httpx.Response) -> LLMResponse:
        try:
            data: dict[str, Any] = response.json()
        except ValueError as exc:
            raise LLMError(f"gemini: response was not JSON: {response.text[:200]}") from exc

        candidates: list[dict[str, Any]] = data.get("candidates") or []
        if not candidates:
            raise LLMError(f"gemini: no candidates in response: {str(data)[:300]}")

        parts: list[dict[str, Any]] = candidates[0].get("content", {}).get("parts") or []
        texts = [p["text"] for p in parts if "text" in p]
        calls = [
            ToolCall(name=p["functionCall"]["name"], arguments=p["functionCall"].get("args") or {})
            for p in parts
            if "functionCall" in p
        ]
        return LLMResponse(
            model=data.get("modelVersion") or self.model,
            text="\n".join(texts) if texts else None,
            tool_calls=calls,
            finish_reason=candidates[0].get("finishReason"),
        )
