"""Tests for the AI Provider abstraction (`app.services.ai_provider`).

Uses `httpx.MockTransport` to simulate the OpenAI API without any real
network access — covers success, timeout, transient server error + retry,
non-retryable error, rate limit, and invalid structured output."""

from __future__ import annotations

import json

import httpx
import pytest

from app.services.ai_provider import (
    MAX_OUTPUT_CHARS,
    AIProviderTimeoutError,
    AIProviderUnavailableError,
    AIRateLimitError,
    AIResponseValidationError,
    OpenAIProvider,
)


def _json_response(status_code: int, payload: dict) -> httpx.Response:
    return httpx.Response(status_code, json=payload)


def _chat_completion(content: str) -> dict:
    return {"choices": [{"message": {"content": content}}]}


class TestGenerateTwinResponseSuccess:
    @pytest.mark.anyio
    async def test_valid_structured_response_is_parsed(self):
        structured_json = json.dumps(
            {
                "reply": "In deinen letzten Einträgen zeigt sich ein stabiler Schlafrhythmus.",
                "sources": [{"type": "trend", "label": "Deine Schlafdaten"}],
                "needs_more_data": False,
            }
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return _json_response(200, _chat_completion(structured_json))

        provider = OpenAIProvider(api_key="test-key", transport=httpx.MockTransport(handler))
        result = await provider.generate_twin_response(system_prompt="system", user_message="Wie war mein Schlaf?")
        assert "Schlafrhythmus" in result.reply
        assert result.sources[0].type == "trend"
        assert result.needs_more_data is False

    @pytest.mark.anyio
    async def test_overlong_reply_is_truncated(self):
        long_reply = "x" * (MAX_OUTPUT_CHARS + 500)
        structured_json = json.dumps({"reply": long_reply, "sources": [], "needs_more_data": False})

        def handler(request: httpx.Request) -> httpx.Response:
            return _json_response(200, _chat_completion(structured_json))

        provider = OpenAIProvider(api_key="test-key", transport=httpx.MockTransport(handler))
        result = await provider.generate_twin_response(system_prompt="system", user_message="Frage")
        assert len(result.reply) == MAX_OUTPUT_CHARS


class TestMissingApiKey:
    @pytest.mark.anyio
    async def test_raises_unavailable_without_api_key(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        provider = OpenAIProvider(api_key=None)
        with pytest.raises(AIProviderUnavailableError):
            await provider.generate_twin_response(system_prompt="system", user_message="Frage")


class TestTimeout:
    @pytest.mark.anyio
    async def test_timeout_raises_after_retries_exhausted(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.TimeoutException("timed out")

        provider = OpenAIProvider(api_key="test-key", transport=httpx.MockTransport(handler), max_retries=1)
        with pytest.raises(AIProviderTimeoutError):
            await provider.generate_twin_response(system_prompt="system", user_message="Frage")


class TestServerErrorRetry:
    @pytest.mark.anyio
    async def test_transient_500_then_success_is_retried(self):
        calls = {"count": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["count"] += 1
            if calls["count"] == 1:
                return httpx.Response(500, json={"error": "server error"})
            structured_json = json.dumps({"reply": "Alles gut.", "sources": [], "needs_more_data": False})
            return _json_response(200, _chat_completion(structured_json))

        provider = OpenAIProvider(api_key="test-key", transport=httpx.MockTransport(handler), max_retries=1)
        result = await provider.generate_twin_response(system_prompt="system", user_message="Frage")
        assert result.reply == "Alles gut."
        assert calls["count"] == 2

    @pytest.mark.anyio
    async def test_persistent_500_raises_after_max_retries(self):
        calls = {"count": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["count"] += 1
            return httpx.Response(500, json={"error": "server error"})

        provider = OpenAIProvider(api_key="test-key", transport=httpx.MockTransport(handler), max_retries=1)
        with pytest.raises(AIProviderUnavailableError):
            await provider.generate_twin_response(system_prompt="system", user_message="Frage")
        assert calls["count"] == 2  # 1 initial + 1 retry, never more (controlled retries)

    @pytest.mark.anyio
    async def test_non_retryable_4xx_is_not_retried(self):
        calls = {"count": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["count"] += 1
            return httpx.Response(400, json={"error": "bad request"})

        provider = OpenAIProvider(api_key="test-key", transport=httpx.MockTransport(handler), max_retries=1)
        with pytest.raises(AIProviderUnavailableError):
            await provider.generate_twin_response(system_prompt="system", user_message="Frage")
        assert calls["count"] == 1  # no retry on a non-retryable client error


class TestRateLimit:
    @pytest.mark.anyio
    async def test_429_raises_rate_limit_error_immediately(self):
        calls = {"count": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["count"] += 1
            return httpx.Response(429, json={"error": "rate limited"})

        provider = OpenAIProvider(api_key="test-key", transport=httpx.MockTransport(handler), max_retries=1)
        with pytest.raises(AIRateLimitError):
            await provider.generate_twin_response(system_prompt="system", user_message="Frage")
        assert calls["count"] == 1  # 429 is not retried by the provider


class TestInvalidStructuredResponse:
    @pytest.mark.anyio
    async def test_non_json_content_raises_validation_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return _json_response(200, _chat_completion("das ist kein JSON"))

        provider = OpenAIProvider(api_key="test-key", transport=httpx.MockTransport(handler))
        with pytest.raises(AIResponseValidationError):
            await provider.generate_twin_response(system_prompt="system", user_message="Frage")

    @pytest.mark.anyio
    async def test_json_missing_required_field_raises_validation_error(self):
        structured_json = json.dumps({"sources": [], "needs_more_data": False})  # missing "reply"

        def handler(request: httpx.Request) -> httpx.Response:
            return _json_response(200, _chat_completion(structured_json))

        provider = OpenAIProvider(api_key="test-key", transport=httpx.MockTransport(handler))
        with pytest.raises(AIResponseValidationError):
            await provider.generate_twin_response(system_prompt="system", user_message="Frage")

    @pytest.mark.anyio
    async def test_unknown_source_type_raises_validation_error(self):
        structured_json = json.dumps(
            {"reply": "Antwort", "sources": [{"type": "made_up_type", "label": "..."}], "needs_more_data": False}
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return _json_response(200, _chat_completion(structured_json))

        provider = OpenAIProvider(api_key="test-key", transport=httpx.MockTransport(handler))
        with pytest.raises(AIResponseValidationError):
            await provider.generate_twin_response(system_prompt="system", user_message="Frage")


class TestSummarizeRelevantContext:
    @pytest.mark.anyio
    async def test_falls_back_to_truncation_on_provider_failure(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.TimeoutException("timed out")

        provider = OpenAIProvider(api_key="test-key", transport=httpx.MockTransport(handler), max_retries=0)
        long_text = "Das ist ein sehr langer Kontext. " * 20
        summary = await provider.summarize_relevant_context(text=long_text, max_chars=50)
        assert summary == long_text[:50]
        assert len(summary) == 50

    @pytest.mark.anyio
    async def test_successful_summary_is_capped_at_max_chars(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return _json_response(200, _chat_completion("x" * 200))

        provider = OpenAIProvider(api_key="test-key", transport=httpx.MockTransport(handler))
        summary = await provider.summarize_relevant_context(text="beliebiger Kontext", max_chars=50)
        assert len(summary) == 50


@pytest.fixture
def anyio_backend():
    return "asyncio"
