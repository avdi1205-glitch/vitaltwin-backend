"""Privacy, export, and consent helpers.

Twin Intelligence Core — Etappe 9.

Pure functions — no database access. Callers (`routers/privacy.py`,
`routers/profile.py::export_profile`) fetch already-`email`-scoped rows and
pass them here for shaping (CSV conversion, consent-status resolution,
export-size guarding).
"""

from __future__ import annotations

import csv
import io
from datetime import datetime

from ..core.validation import MAX_SYNC_EXPORT_ROWS


def rows_to_csv(rows: list[dict[str, object]]) -> str:
    """Converts a flat list of dict rows into CSV text. Column set is the
    union of keys across all rows (rows may have slightly different keys,
    e.g. optional fields) so no data is silently dropped. Returns an empty
    string for an empty list — never fabricates a header for data that
    doesn't exist."""
    if not rows:
        return ""

    fieldnames: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row.keys():
            if key not in seen:
                seen.add(key)
                fieldnames.append(key)

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({key: ("" if row.get(key) is None else row.get(key)) for key in fieldnames})
    return buffer.getvalue()


def resolve_current_consents(consent_rows: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    """Etappe 9 §3: consent is stored as an append-only log (one row per
    grant/revoke decision) — this resolves the *current* status per
    `consent_type` as the most recent row by `created_at`. A revoke is
    therefore always technically effective going forward: the next context
    build / export / any consent check reads this resolved view, never the
    full history, so a revoked consent can never be silently "re-activated"
    by an old row."""
    latest_by_type: dict[str, dict[str, object]] = {}
    for row in consent_rows:
        consent_type = row.get("consent_type")
        if not consent_type:
            continue
        consent_type = str(consent_type)
        created_at = row.get("created_at")
        current = latest_by_type.get(consent_type)
        if current is None or _is_newer(created_at, current.get("created_at")):
            latest_by_type[consent_type] = row

    return {
        consent_type: {
            "granted": bool(row.get("granted")),
            "changed_at": row.get("granted_at") or row.get("revoked_at") or row.get("created_at"),
        }
        for consent_type, row in latest_by_type.items()
    }


def _is_newer(candidate: object, current: object) -> bool:
    if current is None:
        return True
    if candidate is None:
        return False
    try:
        candidate_dt = datetime.fromisoformat(str(candidate).replace("Z", "+00:00"))
        current_dt = datetime.fromisoformat(str(current).replace("Z", "+00:00"))
        return candidate_dt > current_dt
    except ValueError:
        return str(candidate) > str(current)


def count_total_export_rows(bundle: dict[str, object]) -> int:
    total = 0
    for value in bundle.values():
        if isinstance(value, list):
            total += len(value)
    return total


def exceeds_sync_export_limit(total_rows: int) -> bool:
    return total_rows > MAX_SYNC_EXPORT_ROWS
