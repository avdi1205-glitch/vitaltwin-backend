"""Shared-secret authentication for server-to-server webhook endpoints
(CI/CD pipelines, external cron schedulers) that cannot hold/refresh an
admin JWT session the way a human founder can.

Deliberately narrow: each webhook checks its OWN dedicated secret env var
(never a shared "master" secret across unrelated integrations), and never
falls back to "open" if the env var is unset — an unconfigured webhook is
disabled (503), not implicitly public.
"""

from __future__ import annotations

import hmac
import os

from fastapi import HTTPException


def require_webhook_secret(provided: str | None, env_var_name: str) -> None:
    expected = os.getenv(env_var_name, "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail=f"{env_var_name} ist nicht konfiguriert — Webhook ist deaktiviert.")
    if not provided or not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="Ungültiges oder fehlendes Webhook-Secret.")
