"""Unit tests for `app.services.twin_longitudinal_comparison`. Pure
functions over already-built snapshot state dicts — no database access."""

from __future__ import annotations

from app.services import twin_longitudinal_comparison as tlc
from app.services import twin_state_snapshot as tss
from app.services import unified_twin_state as uts
from datetime import date

TODAY = date(2026, 8, 11)


def _state_with_sleep(avg: float):
    entries = [{"entry_date": (TODAY.replace(day=d)).isoformat(), "sleep_hours": avg} for d in range(1, 6)]
    return uts.build_unified_twin_state(
        profile=None, daily_entries=entries, goals=[], habits=[], confirmed_memories=[], confirmed_patterns=[],
        google_health=None, cgm=None, nutrition=None, biomarker_calculations=[], today=TODAY,
    )


def _empty_state():
    return uts.build_unified_twin_state(
        profile=None, daily_entries=[], goals=[], habits=[], confirmed_memories=[], confirmed_patterns=[],
        google_health=None, cgm=None, nutrition=None, biomarker_calculations=[], today=TODAY,
    )


class TestCompareSnapshots:
    def test_missing_historical_state_is_honest(self):
        newer = tss.build_snapshot_state(_state_with_sleep(7.0))
        result = tlc.compare_snapshots(None, newer)
        assert result.available is False
        assert result.explanations == []

    def test_trend_increase_wording_is_non_medical(self):
        older = tss.build_snapshot_state(_state_with_sleep(6.0))
        newer = tss.build_snapshot_state(_state_with_sleep(7.5))
        result = tlc.compare_snapshots(older, newer)
        assert result.available is True
        joined = " ".join(result.explanations).lower()
        assert "gestiegen" in joined
        for forbidden in ("verbessert", "gesünder", "risiko", "krankheit", "diagnose"):
            assert forbidden not in joined

    def test_trend_decrease_wording(self):
        older = tss.build_snapshot_state(_state_with_sleep(7.5))
        newer = tss.build_snapshot_state(_state_with_sleep(6.0))
        result = tlc.compare_snapshots(older, newer)
        assert any("gesunken" in text for text in result.explanations)

    def test_domain_added_wording(self):
        older = tss.build_snapshot_state(_empty_state())
        newer = tss.build_snapshot_state(_state_with_sleep(7.0))
        result = tlc.compare_snapshots(older, newer)
        assert any("neu verfügbar" in text for text in result.explanations)

    def test_domain_removed_wording(self):
        older = tss.build_snapshot_state(_state_with_sleep(7.0))
        newer = tss.build_snapshot_state(_empty_state())
        result = tlc.compare_snapshots(older, newer)
        assert any("nicht mehr unterstützt" in text for text in result.explanations)

    def test_biomarker_updated_wording(self):
        calc_old = {"created_at": "2026-08-01T00:00:00+00:00", "biologisches_alter": 40.0, "differenz": 0.0, "marker_breakdown": []}
        calc_new = {"created_at": "2026-08-10T00:00:00+00:00", "biologisches_alter": 39.0, "differenz": -1.0, "marker_breakdown": []}
        base = dict(profile=None, daily_entries=[], goals=[], habits=[], confirmed_memories=[], confirmed_patterns=[], google_health=None, cgm=None, nutrition=None, today=TODAY)
        older = tss.build_snapshot_state(uts.build_unified_twin_state(biomarker_calculations=[calc_old], **base))
        newer = tss.build_snapshot_state(uts.build_unified_twin_state(biomarker_calculations=[calc_old, calc_new], **base))
        result = tlc.compare_snapshots(older, newer)
        assert any("aktualisiert" in text for text in result.explanations)

    def test_no_forbidden_medical_wording_across_all_categories(self):
        older = tss.build_snapshot_state(_empty_state())
        newer = tss.build_snapshot_state(_state_with_sleep(7.0))
        result = tlc.compare_snapshots(older, newer)
        joined = " ".join(result.explanations).lower()
        for forbidden in ("gesundheit hat sich verbessert", "risiko gestiegen", "krankheitswahrscheinlichkeit"):
            assert forbidden not in joined

    def test_no_changes_yields_empty_explanations(self):
        state = tss.build_snapshot_state(_state_with_sleep(7.0))
        result = tlc.compare_snapshots(state, state)
        assert result.available is True
        assert result.explanations == []


class TestCompareBehavioralBaseline:
    def test_missing_historical_state_is_honest(self):
        result = tlc.compare_behavioral_baseline(None, tss.build_snapshot_state(_state_with_sleep(7.0)))
        assert result["available"] is False

    def test_delta_computed_correctly(self):
        older = tss.build_snapshot_state(_state_with_sleep(6.0))
        newer = tss.build_snapshot_state(_state_with_sleep(7.5))
        result = tlc.compare_behavioral_baseline(older, newer)
        assert result["available"] is True
        assert result["fields"]["sleep_hours"]["earlier_average"] == 6.0
        assert result["fields"]["sleep_hours"]["current_average"] == 7.5
        assert result["fields"]["sleep_hours"]["delta"] == 1.5

    def test_no_comparable_fields_is_honest(self):
        older = tss.build_snapshot_state(_empty_state())
        newer = tss.build_snapshot_state(_empty_state())
        result = tlc.compare_behavioral_baseline(older, newer)
        assert result["available"] is False

    def test_data_quality_then_vs_now_included(self):
        older = tss.build_snapshot_state(_state_with_sleep(6.0))
        newer = tss.build_snapshot_state(_state_with_sleep(7.5))
        result = tlc.compare_behavioral_baseline(older, newer)
        assert "earlier_data_quality" in result
        assert "current_data_quality" in result
