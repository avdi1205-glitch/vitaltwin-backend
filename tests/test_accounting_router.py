"""Router-level tests for `app.routers.accounting` — permission gating
(both new endpoints require `view_accounting`/`manage_accounting`,
super_admin-only per `core/admin_rbac.py`), overview aggregation, CSV
import error handling, and export dispatch (incl. DATEV). Follows the
same direct-async-call + permission-spy pattern as
`test_founder_ceo_intelligence.py`."""

from __future__ import annotations

import io
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, UploadFile

from app.routers import accounting as accounting_router


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def permission_spy(monkeypatch):
    calls: list[tuple] = []

    def _fake(authorization, permission):
        calls.append((authorization, permission))
        return SimpleNamespace(email="founder@example.com", role="super_admin")

    monkeypatch.setattr(accounting_router, "require_admin_permission", _fake)
    return calls


def _upload(content: bytes, filename: str = "report.csv") -> UploadFile:
    return UploadFile(io.BytesIO(content), filename=filename)


class TestOverview:
    @pytest.mark.anyio
    async def test_requires_view_accounting_permission(self, monkeypatch, permission_spy):
        monkeypatch.setattr(accounting_router.stripe_billing, "get_revenue_summary", lambda: {})
        monkeypatch.setattr(accounting_router.adsense_billing, "get_earnings_summary", lambda: {})
        await accounting_router.accounting_overview(authorization="Bearer x")
        assert permission_spy == [("Bearer x", "view_accounting")]

    @pytest.mark.anyio
    async def test_combines_stripe_and_adsense_summaries(self, monkeypatch, permission_spy):
        monkeypatch.setattr(accounting_router.stripe_billing, "get_revenue_summary", lambda: {"revenue_month": 12.0})
        monkeypatch.setattr(accounting_router.adsense_billing, "get_earnings_summary", lambda: {"earnings_month": 3.0})
        result = await accounting_router.accounting_overview(authorization="Bearer x")
        assert result == {"stripe": {"revenue_month": 12.0}, "adsense": {"earnings_month": 3.0}}


class TestAdsenseImport:
    @pytest.mark.anyio
    async def test_requires_manage_accounting_permission(self, monkeypatch, permission_spy):
        monkeypatch.setattr(
            accounting_router.adsense_billing,
            "import_earnings_csv",
            lambda **kwargs: {"batch_id": 1, "rows_imported": 1, "rows_skipped_duplicate": 0, "rows_skipped_other": 0, "note": ""},
        )
        await accounting_router.import_adsense_csv(file=_upload(b"data"), authorization="Bearer x")
        assert permission_spy == [("Bearer x", "manage_accounting")]

    @pytest.mark.anyio
    async def test_empty_file_returns_400(self, permission_spy):
        with pytest.raises(HTTPException) as exc_info:
            await accounting_router.import_adsense_csv(file=_upload(b""), authorization="Bearer x")
        assert exc_info.value.status_code == 400

    @pytest.mark.anyio
    async def test_invalid_csv_returns_400_with_parser_message(self, monkeypatch, permission_spy):
        def _raise(**kwargs):
            raise ValueError("Keine Einnahmen-Spalte gefunden.")

        monkeypatch.setattr(accounting_router.adsense_billing, "import_earnings_csv", _raise)
        with pytest.raises(HTTPException) as exc_info:
            await accounting_router.import_adsense_csv(file=_upload(b"Date,Country\n1,2\n"), authorization="Bearer x")
        assert exc_info.value.status_code == 400
        assert "Einnahmen-Spalte" in exc_info.value.detail

    @pytest.mark.anyio
    async def test_file_too_large_returns_400(self, permission_spy):
        oversized = b"x" * (accounting_router.MAX_IMPORT_FILE_BYTES + 1)
        with pytest.raises(HTTPException) as exc_info:
            await accounting_router.import_adsense_csv(file=_upload(oversized), authorization="Bearer x")
        assert exc_info.value.status_code == 400


class TestExport:
    @pytest.mark.anyio
    async def test_unknown_source_returns_400(self, permission_spy):
        with pytest.raises(HTTPException) as exc_info:
            await accounting_router.export_accounting_data(source="not_real", authorization="Bearer x")
        assert exc_info.value.status_code == 400

    @pytest.mark.anyio
    async def test_invalid_format_returns_400(self, permission_spy):
        with pytest.raises(HTTPException) as exc_info:
            await accounting_router.export_accounting_data(source="stripe_payments", format="xml", authorization="Bearer x")
        assert exc_info.value.status_code == 400

    @pytest.mark.anyio
    async def test_datev_export_uses_view_permission_and_returns_disclaimer(self, monkeypatch, permission_spy):
        monkeypatch.setattr(
            accounting_router.accounting_export,
            "export_datev_buchungsstapel",
            lambda **kwargs: {"format": "datev_extf_buchungsstapel", "row_count": 0, "csv": "", "disclaimer": "draft"},
        )
        result = await accounting_router.export_accounting_data(source="datev", authorization="Bearer x")
        assert result["disclaimer"] == "draft"
        assert permission_spy == [("Bearer x", "view_accounting")]

    @pytest.mark.anyio
    async def test_json_format_returns_raw_rows(self, monkeypatch, permission_spy):
        monkeypatch.setitem(accounting_router.accounting_export.SOURCE_FETCHERS, "stripe_payments", lambda s, e: [{"a": 1}])
        result = await accounting_router.export_accounting_data(source="stripe_payments", format="json", authorization="Bearer x")
        assert result == {"source": "stripe_payments", "row_count": 1, "rows": [{"a": 1}]}
