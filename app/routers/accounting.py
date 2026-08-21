"""Accounting / Buchhaltungs-Grundlage — GoBD-oriented read-only export
and AdSense earnings import (VitalTwin Enterprise, Admin Control Center
follow-up, 2026-08-21).

Mounted at `/api/admin/accounting` in `app/main.py` (own file, same
pattern as `affiliate_admin.py` mounted at `/api/admin/affiliate` — keeps
`routers/admin.py` from growing further). Uses two new, narrow
permissions (`view_accounting`/`manage_accounting`), granted only to
`super_admin` by default — see `core/admin_rbac.py` for the rationale
(real financial/tax data destined for direct Steuerberater handover).

Does NOT touch the existing Stripe webhook flow
(`routers/payments.py`/`core/stripe_billing.py`) at all — purely
additive: reads the same `vt_stripe_*` tables plus the new
`vt_adsense_earnings`/`vt_adsense_import_batches` tables (migration 042).

Affiliate revenue is deliberately NOT part of this module — no real
affiliate program is connected yet (see `docs`/admin business overview's
own `affiliate_note`); this structure can be extended with a third
source once one exists.
"""

from __future__ import annotations

from fastapi import APIRouter, File, Header, HTTPException, UploadFile

from ..core import accounting_export, adsense_billing, stripe_billing
from ..core.admin_rbac import require_admin_permission
from ..core.audit import record_audit_event

router = APIRouter()

MAX_IMPORT_FILE_BYTES = 5 * 1024 * 1024  # 5 MB -- generous for a daily/monthly AdSense CSV export.


@router.get("/overview")
async def accounting_overview(authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_accounting")
    return {
        "stripe": stripe_billing.get_revenue_summary(),
        "adsense": adsense_billing.get_earnings_summary(),
    }


@router.post("/adsense/import")
async def import_adsense_csv(file: UploadFile = File(...), authorization: str | None = Header(default=None)):
    admin = require_admin_permission(authorization, "manage_accounting")

    raw_bytes = await file.read()
    if not raw_bytes:
        raise HTTPException(status_code=400, detail="Datei ist leer.")
    if len(raw_bytes) > MAX_IMPORT_FILE_BYTES:
        raise HTTPException(status_code=400, detail="Datei zu groß (max. 5 MB).")

    try:
        result = adsense_billing.import_earnings_csv(raw_bytes=raw_bytes, filename=file.filename, imported_by=admin.email)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    record_audit_event(
        user_id=None,
        email=admin.email,
        action="create",
        entity_type="adsense_import",
        entity_id=str(result.get("batch_id")) if result.get("batch_id") else None,
        metadata={"filename": file.filename, "rows_imported": result.get("rows_imported")},
    )
    return result


@router.get("/adsense/earnings")
async def list_adsense_earnings(
    start_date: str | None = None, end_date: str | None = None, authorization: str | None = Header(default=None)
):
    require_admin_permission(authorization, "view_accounting")
    rows = adsense_billing.list_earnings(start_date, end_date)
    return {"rows": rows, "row_count": len(rows)}


@router.get("/adsense/import-batches")
async def list_adsense_import_batches(authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_accounting")
    return {"batches": adsense_billing.list_import_batches()}


@router.get("/export")
async def export_accounting_data(
    source: str,
    format: str = "csv",
    start_date: str | None = None,
    end_date: str | None = None,
    berater_nr: str = "",
    mandant_nr: str = "",
    wj_beginn: str = "",
    sachkontenlaenge: int = 4,
    erloes_konto: str = "",
    adsense_konto: str = "",
    erloesschmaelerung_konto: str = "",
    gegenkonto: str = "",
    authorization: str | None = Header(default=None),
):
    """Read-only, never modifies any source table. `source=datev` returns
    the DATEV Buchungsstapel draft (see `accounting_export.DATEV_FORMAT_DISCLAIMER`
    — Konto/Berater/Mandant values are blank unless explicitly passed)."""
    admin = require_admin_permission(authorization, "view_accounting")

    if source == "datev":
        result = accounting_export.export_datev_buchungsstapel(
            start_date=start_date,
            end_date=end_date,
            berater_nr=berater_nr,
            mandant_nr=mandant_nr,
            wj_beginn=wj_beginn,
            sachkontenlaenge=sachkontenlaenge,
            erloes_konto=erloes_konto,
            adsense_konto=adsense_konto,
            erloesschmaelerung_konto=erloesschmaelerung_konto,
            gegenkonto=gegenkonto,
        )
    else:
        if format not in ("csv", "json"):
            raise HTTPException(status_code=400, detail="format muss 'csv' oder 'json' sein.")
        fetcher = accounting_export.SOURCE_FETCHERS.get(source)
        if fetcher is None:
            allowed = ", ".join(sorted(accounting_export.SOURCE_FETCHERS) + ["datev"])
            raise HTTPException(status_code=400, detail=f"Unbekannte Quelle. Erlaubt: {allowed}")
        if format == "json":
            rows = fetcher(start_date, end_date)
            result = {"source": source, "row_count": len(rows), "rows": rows}
        else:
            result = accounting_export.export_csv(source, start_date, end_date)

    record_audit_event(
        user_id=None,
        email=admin.email,
        action="export_request",
        entity_type="accounting_export",
        metadata={"source": source, "format": format, "start_date": start_date, "end_date": end_date},
    )
    return result
