"""Unit tests for `app.services.privacy_export`. Pure functions — no
network/database access."""

from __future__ import annotations

from app.services.privacy_export import (
    count_total_export_rows,
    exceeds_sync_export_limit,
    resolve_current_consents,
    rows_to_csv,
)


class TestRowsToCsv:
    def test_empty_list_yields_empty_string(self):
        assert rows_to_csv([]) == ""

    def test_single_row_produces_header_and_row(self):
        csv_text = rows_to_csv([{"a": 1, "b": "x"}])
        lines = csv_text.strip().splitlines()
        assert lines[0] == "a,b"
        assert lines[1] == "1,x"

    def test_union_of_keys_across_heterogeneous_rows(self):
        csv_text = rows_to_csv([{"a": 1}, {"a": 2, "b": "extra"}])
        header = csv_text.strip().splitlines()[0]
        assert "a" in header
        assert "b" in header

    def test_none_values_become_empty_cells(self):
        csv_text = rows_to_csv([{"a": None, "b": "x"}])
        lines = csv_text.strip().splitlines()
        assert lines[1] == ",x"


class TestResolveCurrentConsents:
    def test_latest_row_per_type_wins(self):
        rows = [
            {"consent_type": "marketing", "granted": True, "created_at": "2026-01-01T00:00:00+00:00"},
            {"consent_type": "marketing", "granted": False, "created_at": "2026-02-01T00:00:00+00:00"},
        ]
        resolved = resolve_current_consents(rows)
        assert resolved["marketing"]["granted"] is False

    def test_different_types_are_independent(self):
        rows = [
            {"consent_type": "marketing", "granted": True, "created_at": "2026-01-01T00:00:00+00:00"},
            {"consent_type": "ai_features", "granted": True, "created_at": "2026-01-01T00:00:00+00:00"},
        ]
        resolved = resolve_current_consents(rows)
        assert resolved["marketing"]["granted"] is True
        assert resolved["ai_features"]["granted"] is True

    def test_no_rows_yields_empty_dict(self):
        assert resolve_current_consents([]) == {}

    def test_revocation_is_technically_effective(self):
        rows = [
            {"consent_type": "chat_storage", "granted": True, "created_at": "2026-01-01T00:00:00+00:00"},
            {
                "consent_type": "chat_storage",
                "granted": False,
                "created_at": "2026-01-05T00:00:00+00:00",
                "revoked_at": "2026-01-05T00:00:00+00:00",
            },
        ]
        resolved = resolve_current_consents(rows)
        assert resolved["chat_storage"]["granted"] is False
        assert resolved["chat_storage"]["changed_at"] == "2026-01-05T00:00:00+00:00"


class TestExportSizeGuard:
    def test_count_total_export_rows_sums_all_lists(self):
        bundle = {"a": [1, 2, 3], "b": [1], "profile": {"not": "a list"}}
        assert count_total_export_rows(bundle) == 4

    def test_exceeds_limit_above_threshold(self):
        assert exceeds_sync_export_limit(5001) is True

    def test_does_not_exceed_limit_at_threshold(self):
        assert exceeds_sync_export_limit(5000) is False
