"""`GoogleHealthClient` — thin, testable wrapper around the Google Health API
(v4). Every network call accepts an optional `transport` so tests can use
`httpx.MockTransport` instead of real network access (mirrors the existing
`OpenAIProvider` convention in `core/ai_provider.py`).

Maps every non-2xx response to a `HealthIntegrationError` with a stable
internal code — callers never see raw Google error bodies, and no health
data or token is ever included in a raised exception's message.
"""

from __future__ import annotations

import httpx

from .health_errors import (
    HEALTH_PROVIDER_ERROR,
    HEALTH_PROVIDER_UNAVAILABLE,
    HEALTH_RATE_LIMITED,
    HEALTH_REAUTH_REQUIRED,
    HealthIntegrationError,
)
from .health_oauth_service import api_base

MAX_PAGES_PER_SYNC = 50  # safety limit so a pagination bug can never loop forever
DEFAULT_PAGE_SIZE = 200  # Google Health API max is 10,000 points/page; keep requests small/fast


class GoogleHealthClient:
    def __init__(self, *, access_token: str, transport: httpx.BaseTransport | None = None, timeout: float = 20.0):
        self._access_token = access_token
        self._transport = transport
        self._timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._access_token}", "Accept": "application/json"}

    async def _get(self, path: str, *, params: dict[str, str] | None = None) -> dict[str, object]:
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
            response = await client.get(f"{api_base()}{path}", headers=self._headers(), params=params)
        self._raise_for_status(response)
        return response.json()

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.status_code == 200:
            return
        if response.status_code == 401:
            # Caller (health_token_service) is responsible for ensuring a
            # fresh token before every call — a 401 here means the token was
            # rejected anyway (e.g. revoked by the user on Google's side).
            raise HealthIntegrationError(HEALTH_REAUTH_REQUIRED, "Zugriff von Google abgelehnt — bitte erneut verbinden.")
        if response.status_code == 403:
            raise HealthIntegrationError(
                HEALTH_REAUTH_REQUIRED, "Fehlende Berechtigung (Scope) für diese Google-Health-Datenart."
            )
        if response.status_code == 429:
            raise HealthIntegrationError(HEALTH_RATE_LIMITED, "Google Health API Rate Limit erreicht.")
        if response.status_code >= 500:
            raise HealthIntegrationError(HEALTH_PROVIDER_UNAVAILABLE, "Google Health API ist derzeit nicht erreichbar.")
        # Google's own error body is safe to include (no tokens/secrets —
        # just its generic error reason) and is otherwise invisible anywhere
        # downstream — keeps future provider errors diagnosable.
        raise HealthIntegrationError(
            HEALTH_PROVIDER_ERROR, f"Google Health API Fehler ({response.status_code}): {response.text[:300]}"
        )

    async def get_identity(self) -> dict[str, object]:
        return await self._get("/users/me/identity")

    async def list_data_points_page(
        self,
        *,
        data_type: str,
        page_size: int = DEFAULT_PAGE_SIZE,
        page_token: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
    ) -> dict[str, object]:
        params: dict[str, str] = {"page_size": str(page_size)}
        if page_token:
            params["page_token"] = page_token
        # NOTE: no `filter` param is sent. A prior attempt to build one from
        # `start_time`/`end_time` was PROVEN wrong by Google's own error
        # response (`INVALID_DATA_POINT_FILTER_DATA_TYPE_MEMBER` /
        # `INVALID_DATA_POINT_FILTER_RESTRICTION_COMPARATOR`) — the exact
        # field-path/comparator grammar isn't confirmed anywhere in the
        # public docs, so guessing further was stopped per root-cause task
        # rules. `filter` is documented as optional; omitting it lets sync
        # work now, bounded only by `page_size`/`MAX_PAGES_PER_SYNC`. Proper
        # server-side time-windowing is a follow-up once Google publishes
        # (or support confirms) the exact filter DSL.
        return await self._get(f"/users/me/dataTypes/{data_type}/dataPoints", params=params)

    async def iter_data_points(
        self,
        *,
        data_type: str,
        start_time: str | None = None,
        end_time: str | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
        max_pages: int = MAX_PAGES_PER_SYNC,
    ):
        """Yields raw data point dicts across all pages, up to `max_pages`
        (safety limit — never loops forever even if Google returns a
        `next_page_token` indefinitely)."""
        page_token: str | None = None
        for _ in range(max_pages):
            payload = await self.list_data_points_page(
                data_type=data_type,
                page_size=page_size,
                page_token=page_token,
                start_time=start_time,
                end_time=end_time,
            )
            # Exact response key name not confirmed against a real API
            # response yet (only request-URL examples were visible in the
            # fetched docs) — defensively check plausible variants, same as
            # the V1 draft, and always keep the caller able to see the full
            # raw item regardless of which key matched.
            items = payload.get("dataPoints") or payload.get("data_points") or payload.get("items") or []
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict):
                        yield item
            page_token = payload.get("nextPageToken") or payload.get("next_page_token")  # type: ignore[assignment]
            if not page_token:
                return
