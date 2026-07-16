"""Lightweight in-memory rate limiting for abuse-prone public endpoints
(register, login, beta applications, feedback).

Deliberately dependency-free (no slowapi/redis) to avoid adding new
libraries for a single-process beta deployment. Known limitation: state is
per-process and resets on restart/deploy, and does not synchronize across
multiple backend instances — sufficient for the current single-instance
Railway deployment, but should be replaced with a shared store (e.g. Redis)
before scaling horizontally.
"""

import time
from collections import defaultdict

from fastapi import HTTPException, Request

_buckets: dict[str, list[float]] = defaultdict(list)


def _client_key(request: Request, scope: str) -> str:
    client_ip = request.client.host if request.client else "unknown"
    return f"{scope}:{client_ip}"


def enforce_rate_limit(request: Request, scope: str, max_requests: int, window_seconds: int = 60) -> None:
    key = _client_key(request, scope)
    now = time.time()
    bucket = _buckets[key]

    while bucket and now - bucket[0] > window_seconds:
        bucket.pop(0)

    if len(bucket) >= max_requests:
        raise HTTPException(status_code=429, detail="Zu viele Anfragen. Bitte versuche es in Kürze erneut.")

    bucket.append(now)
