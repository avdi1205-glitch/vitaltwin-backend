"""Unit tests for `app.core.adsense_billing` — AdSense CSV parsing/import.
Mocks Supabase — no real network/database access. Focuses on: (1) flexible
header matching across locales, (2) all-or-nothing validation (GoBD
Vollständigkeit), (3) duplicate re-import is idempotent (content hash),
(4) summary numbers are real, `None` only when unreachable."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.core import adsense_billing


class _FakeResponse:
    def __init__(self, data):
        self.data = data


class _FakeQuery:
    def __init__(self, store: list[dict]):
        self._store = store
        self._rows: list[dict] | None = None
        self._limit: int | None = None

    def _current(self) -> list[dict]:
        return self._rows if self._rows is not None else list(self._store)

    def select(self, *_a, **_k):
        self._rows = list(self._store)
        return self

    def insert(self, payload):
        rows = payload if isinstance(payload, list) else [payload]
        inserted = []
        for row in rows:
            row = dict(row)
            row.setdefault("id", len(self._store) + 1)
            self._store.append(row)
            inserted.append(row)
        self._rows = inserted
        return self

    def gte(self, field, value):
        self._rows = [r for r in self._current() if str(r.get(field, "")) >= str(value)]
        return self

    def lte(self, field, value):
        self._rows = [r for r in self._current() if str(r.get(field, "")) <= str(value)]
        return self

    def order(self, *_a, **_k):
        return self

    def limit(self, n):
        self._limit = n
        return self

    def execute(self):
        rows = self._current()
        if self._limit is not None:
            rows = rows[: self._limit]
        return _FakeResponse(rows)


class _FakeSupabase:
    def __init__(self):
        self.tables: dict[str, list] = {}

    def table(self, name):
        return _FakeQuery(self.tables.setdefault(name, []))


class _BrokenSupabase:
    def table(self, name):
        raise RuntimeError("unreachable")


@pytest.fixture
def fake_supabase(monkeypatch):
    fake = _FakeSupabase()
    monkeypatch.setattr(adsense_billing, "supabase", fake)
    return fake


SAMPLE_CSV = (
    "Date,Country,Estimated earnings (EUR)\n"
    "20260810,Germany,12.34\n"
    "20260811,Austria,5.67\n"
    "Total,,18.01\n"
)


class TestParseDecimalAmount:
    def test_plain_dot(self):
        assert float(adsense_billing._parse_decimal_amount("12.34")) == pytest.approx(12.34)

    def test_german_decimal_comma(self):
        assert float(adsense_billing._parse_decimal_amount("12,34")) == pytest.approx(12.34)

    def test_german_thousands_and_decimal_comma(self):
        assert float(adsense_billing._parse_decimal_amount("1.234,56")) == pytest.approx(1234.56)

    def test_us_thousands_and_decimal_dot(self):
        assert float(adsense_billing._parse_decimal_amount("1,234.56")) == pytest.approx(1234.56)


class TestParseAdsenseCsv:
    def test_valid_rows_parsed_and_total_row_skipped(self):
        result = adsense_billing.parse_adsense_csv(SAMPLE_CSV)
        assert result.errors == []
        assert len(result.rows) == 2
        assert result.currency == "eur"
        assert result.rows[0]["report_date"] == "2026-08-10"
        assert result.rows[0]["country"] == "Germany"
        assert result.rows[0]["gross_revenue_cents"] == 1234
        assert len(result.skipped_rows) == 1  # the "Total" row

    def test_missing_earnings_column_is_an_error(self):
        result = adsense_billing.parse_adsense_csv("Date,Country\n20260810,Germany\n")
        assert result.errors != []
        assert result.rows == []

    def test_missing_date_column_is_an_error(self):
        result = adsense_billing.parse_adsense_csv("Country,Estimated earnings (EUR)\nGermany,12.34\n")
        assert result.errors != []

    def test_unparseable_amount_rejects_entire_file(self):
        csv_text = "Date,Estimated earnings (EUR)\n20260810,not-a-number\n20260811,5.00\n"
        result = adsense_billing.parse_adsense_csv(csv_text)
        assert result.errors != []
        assert result.rows == []  # all-or-nothing, even though one row was valid

    def test_empty_file_is_an_error(self):
        result = adsense_billing.parse_adsense_csv("")
        assert result.errors == ["Datei ist leer."]


class TestImportEarningsCsv:
    def test_first_import_inserts_rows_and_one_batch(self, fake_supabase):
        result = adsense_billing.import_earnings_csv(raw_bytes=SAMPLE_CSV.encode("utf-8"), filename="report.csv", imported_by="founder@vitaltwin.de")
        assert result["rows_imported"] == 2
        assert result["rows_skipped_duplicate"] == 0
        assert result["batch_id"] is not None
        assert len(fake_supabase.tables[adsense_billing.EARNINGS_TABLE]) == 2
        assert len(fake_supabase.tables[adsense_billing.IMPORT_BATCH_TABLE]) == 1

    def test_reimporting_identical_file_is_skipped_as_duplicate(self, fake_supabase):
        adsense_billing.import_earnings_csv(raw_bytes=SAMPLE_CSV.encode("utf-8"), filename="report.csv", imported_by="founder@vitaltwin.de")
        second = adsense_billing.import_earnings_csv(raw_bytes=SAMPLE_CSV.encode("utf-8"), filename="report.csv", imported_by="founder@vitaltwin.de")
        assert second["rows_imported"] == 0
        assert second["rows_skipped_duplicate"] == 2
        assert second["batch_id"] is None
        # No second batch row was created since nothing new was written.
        assert len(fake_supabase.tables[adsense_billing.IMPORT_BATCH_TABLE]) == 1

    def test_invalid_csv_raises_value_error_and_writes_nothing(self, fake_supabase):
        with pytest.raises(ValueError):
            adsense_billing.import_earnings_csv(raw_bytes=b"Date,Country\n20260810,Germany\n", filename="bad.csv", imported_by="founder@vitaltwin.de")
        assert adsense_billing.EARNINGS_TABLE not in fake_supabase.tables or fake_supabase.tables[adsense_billing.EARNINGS_TABLE] == []
        assert adsense_billing.IMPORT_BATCH_TABLE not in fake_supabase.tables or fake_supabase.tables[adsense_billing.IMPORT_BATCH_TABLE] == []


class TestEarningsSummary:
    def test_empty_but_reachable_table_returns_real_zero(self, fake_supabase):
        summary = adsense_billing.get_earnings_summary()
        assert summary["earnings_month"] == 0
        assert summary["earnings_total"] == 0
        assert summary["note"] == ""

    def test_unreachable_table_returns_none_not_zero(self, monkeypatch):
        monkeypatch.setattr(adsense_billing, "supabase", _BrokenSupabase())
        summary = adsense_billing.get_earnings_summary()
        assert summary["earnings_month"] is None
        assert summary["earnings_total"] is None
        assert summary["note"] != ""

    def test_summary_reflects_imported_rows(self, fake_supabase):
        adsense_billing.import_earnings_csv(raw_bytes=SAMPLE_CSV.encode("utf-8"), filename="report.csv", imported_by="founder@vitaltwin.de")
        summary = adsense_billing.get_earnings_summary()
        assert summary["earnings_total"] == pytest.approx(18.01)
        assert summary["last_import_filename"] == "report.csv"
