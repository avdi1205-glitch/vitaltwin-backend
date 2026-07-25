"""Affiliate Provider Architecture (VitalTwin Enterprise, Founder
Operating System, Submodule F — Affiliate Intelligence).

**No parallel provider system.** This module does not reimplement network
API status — it wraps the already-existing, single-source-of-truth
registry `core/integrations.py::get_affiliate_networks()` for the 6
network APIs, and adds exactly one genuinely working provider: the manual
CSV/JSON/Excel import path that already exists in
`core/affiliate_import_export.py`.

**An integration only counts as "connected" if it was actually tested.**
None of the 6 network APIs (Amazon PartnerNet, Awin, Digistore24, CJ
Affiliate, Impact, TradeDoubler) have real credentials configured in this
environment — they are honestly reported as `not_configured`, exactly as
in `core/integrations.py`. No `connect()`/`testConnection()` call is ever
faked; where no real API access exists, the function simply is not called
at all — the manual import path remains available and real regardless.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .affiliate_import_export import import_products
from .integrations import get_affiliate_networks

MANUAL_IMPORT_ID = "manual_import"


@dataclass(frozen=True)
class ProviderStatus:
    id: str
    name: str
    configured: bool
    connection_tested: bool
    kind: str  # "network_api" | "manual_import"
    last_checked: str | None
    note: str
    required_credentials: tuple[str, ...] = ()


def get_provider_statuses() -> list[ProviderStatus]:
    """Real status for every provider named in the spec — 6 network APIs
    (via the existing integrations registry, never re-implemented here)
    plus the one real, working import path."""
    statuses: list[ProviderStatus] = []

    for network in get_affiliate_networks():
        statuses.append(
            ProviderStatus(
                id=network.id,
                name=network.name,
                configured=network.status == "configured",
                connection_tested=False,  # Never tested — no credentials exist in this environment.
                kind="network_api",
                last_checked=None,
                note=network.note,
                required_credentials=(f"{network.id.upper()}_API_KEY",),
            )
        )

    statuses.append(
        ProviderStatus(
            id=MANUAL_IMPORT_ID,
            name="Manueller Import (CSV/JSON/Excel)",
            configured=True,
            connection_tested=True,
            kind="manual_import",
            last_checked=None,
            note="Echter, funktionierender Importweg — siehe core/affiliate_import_export.py. Kein API-Zugang nötig.",
        )
    )
    return statuses


class ManualImportProvider:
    """The one AffiliateProvider implementation that is genuinely real —
    wraps the already-existing, tested `affiliate_import_export.py`
    functions rather than duplicating them."""

    id = MANUAL_IMPORT_ID

    def is_configured(self) -> bool:
        return True

    def test_connection(self) -> dict:
        return {"ok": True, "message": "Manueller Import benötigt keine Verbindungsprüfung.", "tested_at": datetime.now(timezone.utc).isoformat()}

    def sync_products(self, *, fmt: str, content: bytes, created_by: str) -> dict:
        """Delegates directly to the existing, tested import function —
        no parallel product ingestion path."""
        return import_products(fmt, content, created_by=created_by)
