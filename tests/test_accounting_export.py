"""Unit tests for `app.core.accounting_export` — CSV/DATEV export.
Mocks Supabase and `adsense_billing.list_earnings` — no real network/
database access, and never writes anywhere (export is read-only)."""

from __future__ import annotations

import pytest

from app.core import accounting_export


class _FakeResponse:
    def __init__(self, data):
        self.data = data


class _FakeQuery:
    def __init__(self, rows: list[dict]):
        self._all = rows
        self._rows = list(rows)

    def select(self, *_a, **_k):
        return self

    def order(self, *_a, **_k):
        return self

    def gte(self, field, value):
        self._rows = [r for r in self._rows if str(r.get(field, "")) >= str(value)]
        return self

    def lte(self, field, value):
        self._rows = [r for r in self._rows if str(r.get(field, "")) <= str(value)]
        return self

    def execute(self):
        return _FakeResponse(self._rows)


class _FakeSupabase:
    def __init__(self, tables: dict[str, list[dict]]):
        self._tables = tables

    def table(self, name):
        return _FakeQuery(list(self._tables.get(name, [])))


@pytest.fixture
def fake_tables(monkeypatch):
    tables = {
        accounting_export.PAYMENT_TABLE: [
            {"stripe_invoice_id": "in_1", "amount_paid": 9999, "currency": "eur", "email": "a@x.de", "paid_at": "2026-08-10T10:00:00+00:00"},
        ],
        accounting_export.REFUND_TABLE: [
            {"stripe_refund_id": "re_1", "amount": 500, "currency": "eur", "email": "a@x.de", "created_at": "2026-08-11T10:00:00+00:00"},
        ],
        accounting_export.SUBSCRIPTION_TABLE: [
            {"stripe_subscription_id": "sub_1", "status": "active", "email": "a@x.de", "updated_at": "2026-08-01T00:00:00+00:00"},
        ],
    }
    monkeypatch.setattr(accounting_export, "supabase", _FakeSupabase(tables))
    return tables


@pytest.fixture
def fake_adsense(monkeypatch):
    rows = [
        {"report_date": "2026-08-12", "country": "Germany", "gross_revenue_cents": 4200, "currency": "eur", "import_batch_id": 1},
    ]
    monkeypatch.setattr(accounting_export.adsense_billing, "list_earnings", lambda *a, **k: rows)
    return rows


class TestToDatevAmount:
    def test_cents_to_german_decimal_comma(self):
        assert accounting_export._to_datev_amount(9999) == "99,99"
        assert accounting_export._to_datev_amount(500) == "5,00"


class TestToDatevBelegdatum:
    def test_iso_timestamp_to_ddmm(self):
        assert accounting_export._to_datev_belegdatum("2026-08-10T10:00:00+00:00") == "1008"

    def test_plain_date_to_ddmm(self):
        assert accounting_export._to_datev_belegdatum("2026-08-12") == "1208"

    def test_none_returns_empty_string(self):
        assert accounting_export._to_datev_belegdatum(None) == ""


class TestExportCsv:
    def test_unknown_source_raises_value_error(self):
        with pytest.raises(ValueError):
            accounting_export.export_csv("not_a_real_source", None, None)

    def test_stripe_payments_csv_contains_row(self, fake_tables):
        result = accounting_export.export_csv("stripe_payments", None, None)
        assert result["row_count"] == 1
        assert "in_1" in result["csv"]

    def test_date_range_filters_rows(self, fake_tables):
        result = accounting_export.export_csv("stripe_payments", "2026-09-01", None)
        assert result["row_count"] == 0


class TestExportDatevBuchungsstapel:
    def test_includes_disclaimer_and_all_three_sources(self, fake_tables, fake_adsense):
        result = accounting_export.export_datev_buchungsstapel(start_date=None, end_date=None)
        assert result["format"] == "datev_extf_buchungsstapel"
        assert result["disclaimer"] == accounting_export.DATEV_FORMAT_DISCLAIMER
        assert result["row_count"] == 3  # 1 payment + 1 refund + 1 adsense row
        assert "EXTF" in result["csv"]
        assert "Buchungsstapel" in result["csv"]

    def test_blank_konto_placeholders_when_not_provided(self, fake_tables, fake_adsense):
        result = accounting_export.export_datev_buchungsstapel(start_date=None, end_date=None)
        lines = result["csv"].splitlines()
        # Data rows start after the 2 header rows; Konto (7th field) should
        # be an empty quoted field ("") when no account number was passed.
        assert '""' in lines[2]

    def test_subscriptions_are_not_part_of_datev_export(self, fake_tables, fake_adsense):
        result = accounting_export.export_datev_buchungsstapel(start_date=None, end_date=None)
        assert "sub_1" not in result["csv"]
