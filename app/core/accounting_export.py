"""GoBD-oriented CSV/DATEV export for the tax-advisor handover
(Buchhaltungs-Grundlage, 2026-08-21).

Reads Stripe (`vt_stripe_payments`/`vt_stripe_refunds`/
`vt_stripe_subscriptions`, migration 023) and AdSense
(`vt_adsense_earnings`, migration 042) — never writes to any of them,
and never touches the existing Stripe webhook flow
(`routers/payments.py`/`core/stripe_billing.py`) at all.

The DATEV "Buchungsstapel" (EXTF) writer below is a best-effort
implementation of the publicly documented DATEV ASCII format. It could
not be verified in this session against DATEV's current official spec
(no live access to datev.de) — see `DATEV_FORMAT_DISCLAIMER`, which is
also returned inline in every DATEV export response so it's never buried
in code only. Confirm the exact column/version requirements and, above
all, the real Konto/Gegenkonto/Berater-/Mandanten-Nummer values with a
DATEV-certified Steuerberater/Kanzlei before any productive import —
this module deliberately never invents any of those numbers; they stay
blank unless explicitly passed in by the caller.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from decimal import Decimal

from . import adsense_billing
from .supabase import supabase

PAYMENT_TABLE = "vt_stripe_payments"
REFUND_TABLE = "vt_stripe_refunds"
SUBSCRIPTION_TABLE = "vt_stripe_subscriptions"

DATEV_FORMAT_DISCLAIMER = (
    "Entwurf auf Basis des oeffentlich dokumentierten DATEV-EXTF-Formats (Kategorie 21, "
    "'Buchungsstapel'). Berater-/Mandantennummer, Sachkontenlaenge und alle Kontonummern "
    "sind bewusst Platzhalter (leer, sofern nicht uebergeben) und wurden in dieser Session "
    "NICHT gegen die aktuelle offizielle DATEV-Spezifikation oder von einem DATEV-Steuerberater "
    "geprueft. Vor dem ersten echten Import unbedingt mit einer DATEV-Kanzlei gegenpruefen "
    "(insbesondere Kontenrahmen SKR03/SKR04, Buchungsschluessel und Zeichenkodierung)."
)


def _rows_to_csv(rows: list[dict]) -> str:
    if not rows:
        return ""
    fieldnames = sorted({key for row in rows for key in row.keys()})
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fieldnames)
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buffer.getvalue()


def fetch_stripe_payments(start_date: str | None, end_date: str | None) -> list[dict]:
    try:
        query = supabase.table(PAYMENT_TABLE).select("*").order("paid_at", desc=True)
        if start_date:
            query = query.gte("paid_at", start_date)
        if end_date:
            query = query.lte("paid_at", end_date)
        return query.execute().data or []
    except Exception:
        return []


def fetch_stripe_refunds(start_date: str | None, end_date: str | None) -> list[dict]:
    try:
        query = supabase.table(REFUND_TABLE).select("*").order("created_at", desc=True)
        if start_date:
            query = query.gte("created_at", start_date)
        if end_date:
            query = query.lte("created_at", end_date)
        return query.execute().data or []
    except Exception:
        return []


def fetch_stripe_subscriptions(start_date: str | None, end_date: str | None) -> list[dict]:
    try:
        query = supabase.table(SUBSCRIPTION_TABLE).select("*").order("updated_at", desc=True)
        if start_date:
            query = query.gte("updated_at", start_date)
        if end_date:
            query = query.lte("updated_at", end_date)
        return query.execute().data or []
    except Exception:
        return []


def _fetch_adsense_earnings(start_date: str | None, end_date: str | None) -> list[dict]:
    return adsense_billing.list_earnings(start_date, end_date, limit=10000)


# Single source of truth for the "which source am I exporting" dispatch —
# shared by the plain CSV/JSON export and the router's validation message.
SOURCE_FETCHERS = {
    "stripe_payments": fetch_stripe_payments,
    "stripe_refunds": fetch_stripe_refunds,
    "stripe_subscriptions": fetch_stripe_subscriptions,
    "adsense_earnings": _fetch_adsense_earnings,
}


def export_csv(source: str, start_date: str | None, end_date: str | None) -> dict:
    fetcher = SOURCE_FETCHERS.get(source)
    if fetcher is None:
        raise ValueError(f"Unbekannte Quelle. Erlaubt: {', '.join(sorted(SOURCE_FETCHERS))}")
    rows = fetcher(start_date, end_date)
    return {"source": source, "row_count": len(rows), "csv": _rows_to_csv(rows)}


def _to_datev_amount(cents: int) -> str:
    """`1234` (cents) -> `"12,34"` (German decimal comma, DATEV convention)."""
    value = Decimal(cents) / Decimal(100)
    return f"{value:.2f}".replace(".", ",")


def _to_datev_belegdatum(iso_value: str | None) -> str:
    """DATEV Belegdatum in the Buchungsstapel format is day+month only
    (`ddMM`), no year/separators — the year is implied by the WJ/period
    fields in the header row."""
    if not iso_value:
        return ""
    try:
        parsed = datetime.fromisoformat(iso_value.replace("Z", "+00:00"))
    except ValueError:
        return ""
    return parsed.strftime("%d%m")


def _datev_header_row(
    *,
    berater_nr: str,
    mandant_nr: str,
    wj_beginn: str,
    sachkontenlaenge: int,
    start_date: str | None,
    end_date: str | None,
) -> list[str]:
    generated_at = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")[:-3]
    return [
        "EXTF", "510", "21", "Buchungsstapel", "7", generated_at, "", "VitalTwin", "",
        berater_nr, mandant_nr, wj_beginn.replace("-", ""), str(sachkontenlaenge),
        (start_date or "").replace("-", ""), (end_date or "").replace("-", ""),
        "VitalTwin Buchungsstapel", "", "", "1", "", "0", "EUR",
    ]


_DATEV_COLUMN_HEADER = [
    "Umsatz (ohne Soll/Haben-Kz)", "Soll/Haben-Kennzeichen", "WKZ Umsatz", "Kurs",
    "Basis-Umsatz", "WKZ Basis-Umsatz", "Konto", "Gegenkonto (ohne BU-Schlüssel)",
    "BU-Schlüssel", "Belegdatum", "Belegfeld 1", "Belegfeld 2", "Skonto", "Buchungstext",
]


def export_datev_buchungsstapel(
    *,
    start_date: str | None,
    end_date: str | None,
    berater_nr: str = "",
    mandant_nr: str = "",
    wj_beginn: str = "",
    sachkontenlaenge: int = 4,
    erloes_konto: str = "",
    adsense_konto: str = "",
    erloesschmaelerung_konto: str = "",
    gegenkonto: str = "",
) -> dict:
    """Real money movements only (Stripe payments/refunds + AdSense
    earnings) — deliberately excludes `vt_stripe_subscriptions`, which is
    a status table, not a transaction ledger. See `DATEV_FORMAT_DISCLAIMER`."""
    payments = fetch_stripe_payments(start_date, end_date)
    refunds = fetch_stripe_refunds(start_date, end_date)
    earnings = _fetch_adsense_earnings(start_date, end_date)

    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";", quoting=csv.QUOTE_ALL)
    writer.writerow(
        _datev_header_row(
            berater_nr=berater_nr, mandant_nr=mandant_nr, wj_beginn=wj_beginn,
            sachkontenlaenge=sachkontenlaenge, start_date=start_date, end_date=end_date,
        )
    )
    writer.writerow(_DATEV_COLUMN_HEADER)

    for payment in payments:
        writer.writerow(
            [
                _to_datev_amount(int(payment.get("amount_paid") or 0)),
                "H",
                (payment.get("currency") or "eur").upper(),
                "", "", "",
                erloes_konto,
                gegenkonto,
                "",
                _to_datev_belegdatum(payment.get("paid_at")),
                payment.get("stripe_invoice_id") or "",
                "",
                "",
                f"Stripe Zahlung {payment.get('email') or ''}".strip(),
            ]
        )

    for refund in refunds:
        writer.writerow(
            [
                _to_datev_amount(int(refund.get("amount") or 0)),
                "S",
                (refund.get("currency") or "eur").upper(),
                "", "", "",
                erloesschmaelerung_konto or erloes_konto,
                gegenkonto,
                "",
                _to_datev_belegdatum(refund.get("created_at")),
                refund.get("stripe_refund_id") or "",
                "",
                "",
                f"Stripe Rückerstattung {refund.get('email') or ''}".strip(),
            ]
        )

    for earning in earnings:
        country = earning.get("country") or ""
        writer.writerow(
            [
                _to_datev_amount(int(earning.get("gross_revenue_cents") or 0)),
                "H",
                (earning.get("currency") or "eur").upper(),
                "", "", "",
                adsense_konto,
                gegenkonto,
                "",
                _to_datev_belegdatum(earning.get("report_date")),
                f"batch-{earning.get('import_batch_id') or ''}",
                "",
                "",
                f"AdSense Einnahmen {country} (sonstige betriebliche Ertraege; "
                "Reverse-Charge/Google Ireland -- steuerliche Zuordnung durch Steuerberater)".strip(),
            ]
        )

    return {
        "format": "datev_extf_buchungsstapel",
        "row_count": len(payments) + len(refunds) + len(earnings),
        "csv": buffer.getvalue(),
        "disclaimer": DATEV_FORMAT_DISCLAIMER,
    }
