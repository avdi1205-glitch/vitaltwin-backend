"""Sentry error/performance monitoring — backend (Python/FastAPI).

Inert unless `SENTRY_DSN` is set (same convention as every other optional
integration in this codebase, e.g. `OPENAI_API_KEY`) — no fake/placeholder
DSN is ever used, and `init_sentry()` is a no-op if unconfigured.

Privacy hardening (deliberate, not the SDK's permissive defaults) since this
app processes sensitive health data (CGM readings, nutrition entries, Twin
chat messages):
- `send_default_pii=False` — never auto-attach request user/IP data.
- `max_request_body_size="never"` — request bodies (which can contain CGM
  uploads, nutrition entries, chat messages, or JWTs) are never captured.
- `before_send`/`before_send_transaction` scrub `Authorization`/`Cookie`
  headers defensively, even though PII capture is already off, as a second
  layer of protection against a future SDK default change.
"""

from __future__ import annotations

import os


def _scrub_sensitive_headers(event: dict, _hint: dict) -> dict:
    request = event.get("request")
    if isinstance(request, dict):
        headers = request.get("headers")
        if isinstance(headers, dict):
            for key in list(headers.keys()):
                if key.lower() in ("authorization", "cookie", "set-cookie"):
                    headers[key] = "[Filtered]"
    return event


def init_sentry() -> None:
    dsn = os.getenv("SENTRY_DSN", "").strip()
    if not dsn:
        return

    import sentry_sdk

    sentry_sdk.init(
        dsn=dsn,
        environment=os.getenv("SENTRY_ENVIRONMENT", "production").strip() or "production",
        send_default_pii=False,
        max_request_body_size="never",
        traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0.2") or "0.2"),
        before_send=_scrub_sensitive_headers,
        before_send_transaction=_scrub_sensitive_headers,
    )
