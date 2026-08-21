"""AdSense earnings import — Buchhaltungs-Grundlage (GoBD), 2026-08-21.

Pragmatic first step chosen deliberately over a live AdSense Management
API integration: this codebase has no Google API client library
dependency and no evidence of an AdSense API OAuth scope/credential ever
having been set up (see the accompanying chat report). Building a live
API poll now would mean guessing at Google Cloud project configuration
that cannot be verified from inside this repo. A manual CSV import (an
admin uploads the export AdSense itself already produces) is the
pragmatic path the task explicitly allows for this situation — read-only
with respect to the AdSense account itself (never touches AdSense
settings), and it can be swapped for a scheduled API pull later without
changing the storage shape below.

Populates `vt_adsense_earnings`/`vt_adsense_import_batches` (migration
042) — an append-only ledger, never an upsert-by-date, per GoBD
"Unveränderbarkeit" (see the migration header for the full rationale).
Never touches any `vt_stripe_*` table or the existing Stripe webhook flow.
"""

from __future__ import annotations

import csv
import hashlib
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from .supabase import supabase

IMPORT_BATCH_TABLE = "vt_adsense_import_batches"
EARNINGS_TABLE = "vt_adsense_earnings"

_DATE_COLUMN_KEYWORDS = ("date", "datum")
_COUNTRY_COLUMN_KEYWORDS = ("country", "land")
_EARNINGS_COLUMN_KEYWORDS = ("earning", "einnahme", "revenue", "ertrag")

_DATE_FORMATS = ("%Y%m%d", "%Y-%m-%d", "%d.%m.%Y", "%m/%d/%Y", "%d/%m/%Y", "%b %d, %Y")


@dataclass
class AdsenseParseResult:
    """`errors` non-empty means "import nothing" (GoBD Vollständigkeit —
    no silent partial acceptance of a malformed file). `skipped_rows` is
    informational only (e.g. AdSense's own trailing "Total" summary row,
    which has no date and is expected, not an error)."""

    rows: list[dict] = field(default_factory=list)
    skipped_rows: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    currency: str = "eur"


def _normalize_header(name: str) -> str:
    return name.strip().lower()


def _find_column_index(normalized_header: list[str], keywords: tuple[str, ...]) -> int | None:
    for idx, name in enumerate(normalized_header):
        if any(keyword in name for keyword in keywords):
            return idx
    return None


def _detect_currency(header_label: str) -> str:
    match = re.search(r"\(([A-Za-z]{3})\)", header_label)
    return match.group(1).lower() if match else "eur"


def _parse_date(raw: str) -> date | None:
    raw = raw.strip()
    if not raw:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    try:
        from dateutil import parser as _dateutil_parser  # already a dependency (requirements.txt)

        return _dateutil_parser.parse(raw, dayfirst=True).date()
    except Exception:
        return None


def _parse_decimal_amount(raw: str) -> Decimal:
    raw = raw.strip().replace("\xa0", "").replace(" ", "")
    if not raw:
        raise ValueError("empty amount")
    if "," in raw and "." in raw:
        # Mixed separators: assume whichever comes last is the decimal one
        # (handles both "1.234,56" and "1,234.56").
        if raw.rfind(",") > raw.rfind("."):
            raw = raw.replace(".", "").replace(",", ".")
        else:
            raw = raw.replace(",", "")
    elif "," in raw:
        raw = raw.replace(",", ".")
    return Decimal(raw)


def _decode_csv_bytes(raw_bytes: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return raw_bytes.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw_bytes.decode("utf-8", errors="replace")


def parse_adsense_csv(raw_text: str) -> AdsenseParseResult:
    """Flexible header matching (handles both the English and German
    AdSense UI export locales) — looks for a date column, an optional
    country column, and an earnings column, by keyword rather than an
    exact header name (AdSense lets the user pick which columns to
    include)."""
    non_empty_lines = [line for line in raw_text.splitlines() if line.strip()]
    if not non_empty_lines:
        return AdsenseParseResult(errors=["Datei ist leer."])

    all_records = list(csv.reader(non_empty_lines))
    header = all_records[0]
    normalized_header = [_normalize_header(h) for h in header]

    date_idx = _find_column_index(normalized_header, _DATE_COLUMN_KEYWORDS)
    country_idx = _find_column_index(normalized_header, _COUNTRY_COLUMN_KEYWORDS)
    earnings_idx = _find_column_index(normalized_header, _EARNINGS_COLUMN_KEYWORDS)

    errors: list[str] = []
    if date_idx is None:
        errors.append("Keine Datums-Spalte gefunden (erwartet z. B. 'Date' oder 'Datum').")
    if earnings_idx is None:
        errors.append("Keine Einnahmen-Spalte gefunden (erwartet z. B. 'Estimated earnings (EUR)').")
    if errors:
        return AdsenseParseResult(errors=errors)

    currency = _detect_currency(header[earnings_idx])

    rows: list[dict] = []
    skipped: list[str] = []
    for line_number, (raw_line, record) in enumerate(zip(non_empty_lines[1:], all_records[1:]), start=2):
        if len(record) <= max(date_idx, earnings_idx):
            skipped.append(f"Zeile {line_number}: zu wenige Spalten, übersprungen.")
            continue

        raw_date = record[date_idx].strip()
        parsed_date = _parse_date(raw_date)
        if parsed_date is None:
            # AdSense-Exporte enden ueblicherweise mit einer "Total"-Zeile
            # ohne echtes Datum -- erwartet, kein Fehler.
            skipped.append(f"Zeile {line_number}: kein gültiges Datum ('{raw_date}'), übersprungen (z. B. Summenzeile).")
            continue

        raw_amount = record[earnings_idx].strip()
        try:
            amount = _parse_decimal_amount(raw_amount)
        except (InvalidOperation, ValueError):
            errors.append(f"Zeile {line_number}: Betrag '{raw_amount}' konnte nicht gelesen werden.")
            continue

        country = record[country_idx].strip() if country_idx is not None and len(record) > country_idx else ""
        cents = int((amount * 100).to_integral_value(rounding=ROUND_HALF_UP))

        rows.append(
            {
                "report_date": parsed_date.isoformat(),
                "country": country or None,
                "gross_revenue_cents": cents,
                "currency": currency,
                "raw_row_hash": hashlib.sha256(raw_line.strip().encode("utf-8")).hexdigest(),
            }
        )

    if errors:
        # All-or-nothing: a genuinely malformed value must never result in
        # a silently incomplete import.
        return AdsenseParseResult(skipped_rows=skipped, errors=errors, currency=currency)
    if not rows:
        return AdsenseParseResult(skipped_rows=skipped, errors=["Keine gültigen Datenzeilen gefunden."], currency=currency)
    return AdsenseParseResult(rows=rows, skipped_rows=skipped, currency=currency)


def _existing_hashes_for(report_dates: list[str]) -> set[str]:
    if not report_dates:
        return set()
    try:
        rows = (
            supabase.table(EARNINGS_TABLE)
            .select("raw_row_hash")
            .gte("report_date", min(report_dates))
            .lte("report_date", max(report_dates))
            .execute()
            .data
            or []
        )
    except Exception:
        return set()
    return {r.get("raw_row_hash") for r in rows if r.get("raw_row_hash")}


def import_earnings_csv(*, raw_bytes: bytes, filename: str | None, imported_by: str) -> dict:
    """Raises `ValueError` (caller turns this into an HTTP 400) if the file
    fails validation — nothing is written in that case. Otherwise inserts
    one `vt_adsense_import_batches` row plus one `vt_adsense_earnings` row
    per genuinely new line (exact re-uploads of already-imported rows are
    silently skipped via `raw_row_hash`, not treated as an error)."""
    text = _decode_csv_bytes(raw_bytes)
    result = parse_adsense_csv(text)
    if result.errors:
        raise ValueError("; ".join(result.errors))

    existing_hashes = _existing_hashes_for([row["report_date"] for row in result.rows])
    new_rows = [row for row in result.rows if row["raw_row_hash"] not in existing_hashes]
    duplicate_count = len(result.rows) - len(new_rows)

    if not new_rows:
        return {
            "batch_id": None,
            "rows_imported": 0,
            "rows_skipped_duplicate": duplicate_count,
            "rows_skipped_other": len(result.skipped_rows),
            "note": "Alle Zeilen waren bereits importiert (identischer Inhalt) — keine neue Buchung angelegt.",
        }

    batch_row = {
        "imported_by": imported_by,
        "source_filename": filename,
        "row_count": len(new_rows),
        "skipped_duplicate_count": duplicate_count,
    }
    batch_response = supabase.table(IMPORT_BATCH_TABLE).insert(batch_row).execute()
    batch_id = (batch_response.data or [{}])[0].get("id")
    if batch_id is None:
        raise RuntimeError("Import-Batch konnte nicht gespeichert werden.")

    for row in new_rows:
        row["import_batch_id"] = batch_id
        row["entry_type"] = "original"

    try:
        supabase.table(EARNINGS_TABLE).insert(new_rows).execute()
    except Exception as exc:
        raise ValueError(
            "Import fehlgeschlagen — evtl. enthält die Datei bereits zuvor importierte Zeilen. "
            "Bitte Datei prüfen und erneut versuchen."
        ) from exc

    return {
        "batch_id": batch_id,
        "rows_imported": len(new_rows),
        "rows_skipped_duplicate": duplicate_count,
        "rows_skipped_other": len(result.skipped_rows),
        "note": "",
    }


def list_earnings(start_date: str | None = None, end_date: str | None = None, limit: int = 500) -> list[dict]:
    try:
        query = supabase.table(EARNINGS_TABLE).select("*").order("report_date", desc=True).limit(limit)
        if start_date:
            query = query.gte("report_date", start_date)
        if end_date:
            query = query.lte("report_date", end_date)
        return query.execute().data or []
    except Exception:
        return []


def list_import_batches(limit: int = 50) -> list[dict]:
    try:
        return (
            supabase.table(IMPORT_BATCH_TABLE).select("*").order("created_at", desc=True).limit(limit).execute().data
            or []
        )
    except Exception:
        return []


def get_earnings_summary() -> dict:
    """Mirrors `core/stripe_billing.py::get_revenue_summary` — real
    numbers computed purely from `vt_adsense_earnings`, `None` (not `0`)
    only when the table itself is unreachable/not yet migrated."""
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).date().isoformat()
    try:
        month_rows = supabase.table(EARNINGS_TABLE).select("gross_revenue_cents").gte("report_date", month_start).execute().data or []
        all_rows = supabase.table(EARNINGS_TABLE).select("gross_revenue_cents").execute().data or []
        latest_batches = (
            supabase.table(IMPORT_BATCH_TABLE)
            .select("created_at,source_filename")
            .order("created_at", desc=True)
            .limit(1)
            .execute()
            .data
            or []
        )
    except Exception:
        return {
            "earnings_month": None,
            "earnings_total": None,
            "last_import_at": None,
            "last_import_filename": None,
            "note": "vt_adsense_earnings nicht erreichbar oder Migration 042 noch nicht ausgeführt.",
        }

    earnings_month = sum(int(r.get("gross_revenue_cents") or 0) for r in month_rows) / 100
    earnings_total = sum(int(r.get("gross_revenue_cents") or 0) for r in all_rows) / 100
    last_import = latest_batches[0] if latest_batches else {}
    return {
        "earnings_month": round(earnings_month, 2),
        "earnings_total": round(earnings_total, 2),
        "last_import_at": last_import.get("created_at"),
        "last_import_filename": last_import.get("source_filename"),
        "note": "",
    }
