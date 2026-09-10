"""Both clients must satisfy LLMClient and normalise their provider's shapes correctly."""

import httpx
import pytest

from conftest import FakeClock, FakeTransport
from cua.llm.base import LLMClient, Message, ToolSpec
from cua.llm.gemini import GeminiClient
from cua.llm.groq import GroqClient
from cua.llm.limiter import LLMError, RateLimiter

MESSAGES = [Message(role="system", content="be terse"), Message(role="user", content="hello")]
TOOL = ToolSpec(name="click", description="click a control", parameters={"type": "object"})


def _limiter() -> RateLimiter:
    clock = FakeClock()
    return RateLimiter(monotonic=clock.monotonic, sleep=clock.sleep)


def _gemini(transport: FakeTransport) -> GeminiClient:
    return GeminiClient(api_key="k", client=transport.client(), limiter=_limiter())


def _groq(transport: FakeTransport) -> GroqClient:
    return GroqClient(api_key="k", client=transport.client(), limiter=_limiter())


def test_clients_satisfy_the_protocol() -> None:
    transport = FakeTransport(httpx.Response(200, json={}))
    gemini: LLMClient = _gemini(transport)
    groq: LLMClient = _groq(transport)
    assert isinstance(gemini, LLMClient) and isinstance(groq, LLMClient)


def test_gemini_sends_system_instruction_images_and_tools() -> None:
    transport = FakeTransport(
        httpx.Response(
            200,
            json={
                "modelVersion": "gemini-2.5-flash",
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"text": "clicking"},
                                {"functionCall": {"name": "click", "args": {"id": "7"}}},
                            ]
                        },
                        "finishReason": "STOP",
                    }
                ],
            },
        )
    )
    result = _gemini(transport).complete(MESSAGES, tools=[TOOL], images=[b"\x89PNG"])

    sent = transport.requests[0]
    body = sent.read().decode()
    assert sent.headers["x-goog-api-key"] == "k"
    assert '"systemInstruction"' in body and "be terse" in body
    assert '"functionDeclarations"' in body
    assert '"inlineData"' in body  # the screenshot rides on the newest turn
    assert result.text == "clicking"
    assert [(c.name, c.arguments) for c in result.tool_calls] == [("click", {"id": "7"})]


def test_gemini_reports_an_empty_candidate_list() -> None:
    transport = FakeTransport(httpx.Response(200, json={"candidates": []}))
    with pytest.raises(LLMError, match="no candidates"):
        _gemini(transport).complete(MESSAGES)


def test_groq_parses_openai_tool_calls() -> None:
    transport = FakeTransport(
        httpx.Response(
            200,
            json={
                "model": "llama-3.3-70b-versatile",
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "function": {
                                        "name": "propose_outcome",
                                        "arguments": '{"name": "member_not_found"}',
                                    }
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
            },
        )
    )
    result = _groq(transport).complete(MESSAGES, tools=[TOOL])

    assert transport.requests[0].headers["authorization"] == "Bearer k"
    assert result.text is None
    assert result.tool_calls[0].arguments == {"name": "member_not_found"}


def test_groq_refuses_images() -> None:
    transport = FakeTransport(httpx.Response(200, json={}))
    with pytest.raises(LLMError, match="text-only"):
        _groq(transport).complete(MESSAGES, images=[b"\x89PNG"])
    assert transport.calls == 0


def test_missing_api_key_is_a_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(LLMError, match="GEMINI_API_KEY"):
        GeminiClient()


def test_model_id_comes_from_env_with_a_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_MODEL", "gemini-2.5-flash-lite")
    assert GeminiClient(api_key="k").model == "gemini-2.5-flash-lite"
    monkeypatch.delenv("GEMINI_MODEL")
    assert GeminiClient(api_key="k").model == "gemini-2.5-flash"
