"""Real, on-demand affiliate link health checking.

VitalTwin Enterprise Release — Affiliate Intelligence & Management Platform.

There is no cron/queue system in this codebase (see
`frontend/docs/PLATFORM_ARCHITECTURE.md`), so link checks are **not**
automatic/scheduled — an admin triggers a check per product
(`POST /api/admin/affiliate/products/{id}/check-link`), which performs one
real HTTP request via `httpx`. No result is ever fabricated: a network
error, timeout, or non-2xx/3xx status is honestly recorded as `"broken"`.
"""

from __future__ import annotations

import httpx

REQUEST_TIMEOUT_SECONDS = 6.0


def check_link(url: str) -> dict[str, object]:
    """Performs one real GET request (follows redirects) against `url`.

    Returns `{"link_status": "ok" | "broken", "http_status": int | None,
    "redirected": bool}`. Never raises — a broken link is a normal,
    expected outcome, not an application error.
    """
    try:
        with httpx.Client(follow_redirects=True, timeout=REQUEST_TIMEOUT_SECONDS) as client:
            response = client.get(url)
    except httpx.HTTPError:
        return {"link_status": "broken", "http_status": None, "redirected": False}

    ok = 200 <= response.status_code < 400
    redirected = len(response.history) > 0
    return {
        "link_status": "ok" if ok else "broken",
        "http_status": response.status_code,
        "redirected": redirected,
    }
