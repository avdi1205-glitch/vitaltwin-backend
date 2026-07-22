"""AI Provider abstraction.

Twin Intelligence Core — Etappe 7 §2.

The business logic (`routers/chat.py`, and future callers for weekly-
reflection narratives or recommendation explanations) depends only on the
`AIProvider` interface below — never on a concrete vendor SDK/API directly.
Swapping providers (or adding a second one, e.g. for redundancy) means
writing one new subclass, not touching any router.

Every concrete provider must uphold, at minimum:

- API keys read **only** server-side (never accepted from a request body).
- A hard timeout per call.
- A small, bounded number of controlled retries (only for transient
  failures — timeouts/5xx — never for 4xx, which won't succeed on retry).
- A maximum input length (defensive — the Pydantic request model already
  enforces this, but the provider truncates too, never trusting a single
  layer).
- A maximum output length (defensive — independent of `max_tokens`).
- Structured, schema-validated responses — malformed model output raises
  `AIResponseValidationError`, it is never guessed at or passed through.
- On any failure, an `AIProviderError` (or subclass) is raised — callers
  must never fabricate a reply to paper over a provider failure (Etappe 7
  §2: "keine erfundene Antwort bei API-Ausfall").
"""

from __future__ import annotations

import asyncio
import json
import os
from abc import ABC, abstractmethod

import httpx
from pydantic import BaseModel, ValidationError, field_validator

MAX_INPUT_LENGTH = 500
MAX_OUTPUT_CHARS = 2000
MAX_OUTPUT_TOKENS = 350
REQUEST_TIMEOUT_SECONDS = 20.0
MAX_RETRIES = 1
"""At most 2 total attempts (1 retry) — "kontrollierte Retries", not
unbounded/exponential retry storms that would blow up cost."""
RETRY_BACKOFF_SECONDS = 0.5

MAX_CONCURRENT_REQUESTS = 5
"""Provider-level concurrency cap — a coarse safety net against a runaway
number of parallel calls driving up cost, on top of the per-user daily quota
enforced in `routers/chat.py` via `core/plans.py`."""

OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"
DEFAULT_MODEL = "gpt-4o-mini"

ALLOWED_SOURCE_TYPES = {
    "user_reported",
    "trend",
    "confirmed_memory",
    "pattern",
    "general_wellness_info",
    "uncertain",
    "needs_more_data",
}


class AIProviderError(Exception):
    """Base class for all AI-provider failures."""


class AIProviderUnavailableError(AIProviderError):
    pass


class AIProviderTimeoutError(AIProviderError):
    pass


class AIRateLimitError(AIProviderError):
    pass


class AIResponseValidationError(AIProviderError):
    """The model's raw output didn't parse as the expected structured JSON
    schema (Etappe 7 §2 "Schema-Validierung"). Never surfaced to the user
    directly — the caller falls back to a safe, honest message instead of
    guessing at broken output."""


class TwinAIResponseSource(BaseModel):
    type: str
    label: str

    @field_validator("type")
    @classmethod
    def _validate_type(cls, value: str) -> str:
        if value not in ALLOWED_SOURCE_TYPES:
            raise ValueError(f"Unbekannter Quellentyp: {value}")
        return value


class TwinAIResponse(BaseModel):
    """Etappe 7 §6 transparency contract: every reply must carry its own
    labeled sources — the model is instructed (see
    `twin_conversation.py::build_conversation_system_prompt`) to return
    exactly this shape."""

    reply: str
    sources: list[TwinAIResponseSource] = []
    needs_more_data: bool = False


class AIProvider(ABC):
    """Provider abstraction — the four capabilities named in Etappe 7 §2."""

    @abstractmethod
    async def generate_twin_response(self, *, system_prompt: str, user_message: str) -> TwinAIResponse: ...

    @abstractmethod
    async def generate_weekly_reflection_narrative(self, *, system_prompt: str, context_text: str) -> str: ...

    @abstractmethod
    async def generate_recommendation_explanation(self, *, system_prompt: str, context_text: str) -> str: ...

    @abstractmethod
    async def summarize_relevant_context(self, *, text: str, max_chars: int) -> str: ...


class OpenAIProvider(AIProvider):
    """Concrete `AIProvider` backed by the OpenAI Chat Completions API."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
        max_retries: int = MAX_RETRIES,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._timeout = timeout
        self._max_retries = max_retries
        self._transport = transport
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

    def _resolve_api_key(self) -> str:
        key = (self._api_key or os.getenv("OPENAI_API_KEY", "")).strip()
        if not key:
            raise AIProviderUnavailableError("Der Twin-Chat ist gerade nicht verfügbar (Konfiguration fehlt).")
        return key

    def _resolve_model(self) -> str:
        return (self._model or os.getenv("OPENAI_MODEL", DEFAULT_MODEL)).strip() or DEFAULT_MODEL

    async def _post_with_retries(self, *, headers: dict[str, str], payload: dict[str, object]) -> dict[str, object]:
        last_error: AIProviderError = AIProviderUnavailableError("Der Twin-Chat ist gerade nicht erreichbar.")

        for attempt in range(self._max_retries + 1):
            try:
                async with self._semaphore:
                    async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
                        response = await client.post(OPENAI_API_URL, headers=headers, json=payload)
            except httpx.TimeoutException as exc:
                last_error = AIProviderTimeoutError("Der Twin-Chat antwortet gerade zu langsam.")
                if attempt < self._max_retries:
                    await asyncio.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
                    continue
                raise last_error from exc
            except Exception as exc:
                last_error = AIProviderUnavailableError("Der Twin-Chat ist gerade nicht erreichbar.")
                if attempt < self._max_retries:
                    await asyncio.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
                    continue
                raise last_error from exc

            if response.status_code == 429:
                # Non-retryable here on purpose — the caller/router already
                # enforces per-user daily quotas; retrying a 429 immediately
                # would just make the provider-side limiting worse.
                raise AIRateLimitError("Der Twin-Chat ist gerade stark ausgelastet.")

            if response.status_code >= 500:
                last_error = AIProviderUnavailableError("Der Twin-Chat ist gerade nicht erreichbar.")
                if attempt < self._max_retries:
                    await asyncio.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
                    continue
                raise last_error

            if response.status_code != 200:
                # Non-retryable 4xx (bad request, auth, ...) — retrying
                # would waste cost on a request that can't succeed.
                raise AIProviderUnavailableError("Der Twin-Chat ist gerade nicht erreichbar.")

            try:
                return response.json()
            except Exception as exc:
                raise AIResponseValidationError("Antwort konnte nicht verarbeitet werden.") from exc

        raise last_error

    async def _generate_plain_text(self, *, system_prompt: str, user_message: str) -> str:
        api_key = self._resolve_api_key()
        model = self._resolve_model()
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message[:MAX_INPUT_LENGTH]},
            ],
            "max_tokens": MAX_OUTPUT_TOKENS,
            "temperature": 0.4,
        }
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        data = await self._post_with_retries(headers=headers, payload=payload)
        try:
            text = str(data["choices"][0]["message"]["content"]).strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise AIResponseValidationError("Unerwartetes Antwortformat.") from exc
        return text[:MAX_OUTPUT_CHARS]

    async def generate_twin_response(self, *, system_prompt: str, user_message: str) -> TwinAIResponse:
        api_key = self._resolve_api_key()
        model = self._resolve_model()
        truncated_message = user_message[:MAX_INPUT_LENGTH]

        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": truncated_message},
            ],
            "max_tokens": MAX_OUTPUT_TOKENS,
            "temperature": 0.4,
            "response_format": {"type": "json_object"},
        }
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

        data = await self._post_with_retries(headers=headers, payload=payload)
        try:
            raw_content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise AIResponseValidationError("Unerwartetes Antwortformat.") from exc

        try:
            parsed = json.loads(raw_content)
            structured = TwinAIResponse.model_validate(parsed)
        except (json.JSONDecodeError, ValidationError, TypeError) as exc:
            raise AIResponseValidationError("Antwort entspricht nicht dem erwarteten Schema.") from exc

        if len(structured.reply) > MAX_OUTPUT_CHARS:
            structured = structured.model_copy(update={"reply": structured.reply[:MAX_OUTPUT_CHARS]})
        return structured

    async def generate_weekly_reflection_narrative(self, *, system_prompt: str, context_text: str) -> str:
        return await self._generate_plain_text(system_prompt=system_prompt, user_message=context_text)

    async def generate_recommendation_explanation(self, *, system_prompt: str, context_text: str) -> str:
        return await self._generate_plain_text(system_prompt=system_prompt, user_message=context_text)

    async def summarize_relevant_context(self, *, text: str, max_chars: int) -> str:
        system_prompt = (
            f"Fasse den folgenden Kontext auf maximal {max_chars} Zeichen zusammen, ohne neue Informationen "
            "zu erfinden. Antworte nur mit dem zusammengefassten Text, ohne Einleitung."
        )
        try:
            summary = await self._generate_plain_text(system_prompt=system_prompt, user_message=text)
        except AIProviderError:
            # Never let a summarization failure fabricate content or break
            # the main flow — deterministic truncation is a safe, honest
            # fallback (Etappe 7 §2 "keine erfundene Antwort bei
            # API-Ausfall").
            return text[:max_chars]
        return summary[:max_chars]
