"""Unit tests for `app.services.twin_state_snapshot`. Pure functions over
already-built `UnifiedTwinState`/plain dicts — no database access."""

from __future__ import annotations

from datetime import date

from app.services import twin_state_snapshot as tss
from app.services import unified_twin_state as uts

TODAY = date(2026, 8, 11)


def _empty_state() -> uts.UnifiedTwinState:
    return uts.build_unified_twin_state(
        profile=None, daily_entries=[], goals=[], habits=[], confirmed_memories=[], confirmed_patterns=[],
        google_health=None, cgm=None, nutrition=None, biomarker_calculations=[], today=TODAY,
    )


def _behavioral_state(sleep_avg: float) -> uts.UnifiedTwinState:
    entries = [{"entry_date": (TODAY.replace(day=d)).isoformat(), "sleep_hours": sleep_avg} for d in range(1, 6)]
    return uts.build_unified_twin_state(
        profile=None, daily_entries=entries, goals=[], habits=[], confirmed_memories=[], confirmed_patterns=[],
        google_health=None, cgm=None, nutrition=None, biomarker_calculations=[], today=TODAY,
    )


class TestBuildSnapshotState:
    def test_empty_state_neverhas_any_real_domain(self):
        snapshot = tss.build_snapshot_state(_empty_state())
        assert snapshot["snapshot_version"] == 1
        assert not tss.has_any_real_domain(snapshot)

    def test_never_includes_raw_source_rows(self):
        state = _behavioral_state(7.0)
        snapshot = tss.build_snapshot_state(state)
        serialized = str(snapshot)
        # only small derived numbers/labels should ever appear, never a
        # provider record id, token, or raw per-row payload shape.
        assert "access_token" not in serialized
        assert "refresh_token" not in serialized
        assert "provider_record_name" not in serialized

    def test_deterministic_serialization_same_input_same_output(self):
        state = _behavioral_state(7.0)
        assert tss.build_snapshot_state(state) == tss.build_snapshot_state(state)


class TestDetectMeaningfulChanges:
    def test_first_snapshot_reports_domain_added_for_each_real_domain(self):
        snapshot = tss.build_snapshot_state(_behavioral_state(7.0))
        changes = tss.detect_meaningful_changes(None, snapshot)
        categories = [c["category"] for c in changes]
        assert tss.DOMAIN_ADDED in categories
        assert any(c["domain"] == "behavioral_wellness" for c in changes)

    def test_no_change_reports_nothing(self):
        snapshot = tss.build_snapshot_state(_behavioral_state(7.0))
        assert tss.detect_meaningful_changes(snapshot, snapshot) == []

    def test_tiny_floating_point_difference_is_not_reported(self):
        old = tss.build_snapshot_state(_behavioral_state(7.0))
        new = tss.build_snapshot_state(_behavioral_state(7.05))
        changes = tss.detect_meaningful_changes(old, new)
        assert not any(c["category"] == tss.TREND_CHANGED for c in changes)

    def test_meaningful_trend_change_is_reported(self):
        old = tss.build_snapshot_state(_behavioral_state(7.0))
        new = tss.build_snapshot_state(_behavioral_state(8.0))
        changes = tss.detect_meaningful_changes(old, new)
        trend_changes = [c for c in changes if c["category"] == tss.TREND_CHANGED and c["field"] == "sleep_hours"]
        assert len(trend_changes) == 1
        assert trend_changes[0]["before"] == 7.0
        assert trend_changes[0]["after"] == 8.0

    def test_domain_added_when_previously_missing(self):
        old = tss.build_snapshot_state(_empty_state())
        new = tss.build_snapshot_state(_behavioral_state(7.0))
        changes = tss.detect_meaningful_changes(old, new)
        assert any(c["category"] == tss.DOMAIN_ADDED and c["domain"] == "behavioral_wellness" for c in changes)

    def test_domain_removed_when_now_missing(self):
        old = tss.build_snapshot_state(_behavioral_state(7.0))
        new = tss.build_snapshot_state(_empty_state())
        changes = tss.detect_meaningful_changes(old, new)
        assert any(c["category"] == tss.DOMAIN_REMOVED and c["domain"] == "behavioral_wellness" for c in changes)

    def test_biomarker_updated_on_new_timestamp(self):
        calc_old = {"created_at": "2026-08-01T00:00:00+00:00", "biologisches_alter": 40.0, "differenz": 0.0, "marker_breakdown": []}
        calc_new = {"created_at": "2026-08-10T00:00:00+00:00", "biologisches_alter": 39.0, "differenz": -1.0, "marker_breakdown": []}
        base_kwargs = dict(profile=None, daily_entries=[], goals=[], habits=[], confirmed_memories=[], confirmed_patterns=[], google_health=None, cgm=None, nutrition=None, today=TODAY)
        old = tss.build_snapshot_state(uts.build_unified_twin_state(biomarker_calculations=[calc_old], **base_kwargs))
        new = tss.build_snapshot_state(uts.build_unified_twin_state(biomarker_calculations=[calc_old, calc_new], **base_kwargs))
        changes = tss.detect_meaningful_changes(old, new)
        assert any(c["category"] == tss.BIOMARKER_UPDATED for c in changes)

    def test_memory_changed_on_count_delta(self):
        base_kwargs = dict(profile=None, daily_entries=[], goals=[], habits=[], confirmed_patterns=[], google_health=None, cgm=None, nutrition=None, biomarker_calculations=[], today=TODAY)
        old = tss.build_snapshot_state(uts.build_unified_twin_state(confirmed_memories=[{"human_readable_value": "x"}], **base_kwargs))
        new = tss.build_snapshot_state(uts.build_unified_twin_state(confirmed_memories=[{"human_readable_value": "x"}, {"human_readable_value": "y"}], **base_kwargs))
        changes = tss.detect_meaningful_changes(old, new)
        assert any(c["category"] == tss.MEMORY_CHANGED for c in changes)

    def test_pattern_changed_on_count_delta(self):
        base_kwargs = dict(profile=None, daily_entries=[], goals=[], habits=[], confirmed_memories=[], google_health=None, cgm=None, nutrition=None, biomarker_calculations=[], today=TODAY)
        old = tss.build_snapshot_state(uts.build_unified_twin_state(confirmed_patterns=[{"summary": "x"}], **base_kwargs))
        new = tss.build_snapshot_state(uts.build_unified_twin_state(confirmed_patterns=[{"summary": "x"}, {"summary": "y"}], **base_kwargs))
        changes = tss.detect_meaningful_changes(old, new)
        assert any(c["category"] == tss.PATTERN_CHANGED for c in changes)

    def test_goal_habit_changed_on_new_active_goal(self):
        base_kwargs = dict(profile=None, daily_entries=[], habits=[], confirmed_memories=[], confirmed_patterns=[], google_health=None, cgm=None, nutrition=None, biomarker_calculations=[], today=TODAY)
        old = tss.build_snapshot_state(uts.build_unified_twin_state(goals=[{"status": "active"}], **base_kwargs))
        new = tss.build_snapshot_state(uts.build_unified_twin_state(goals=[{"status": "active"}, {"status": "active"}], **base_kwargs))
        changes = tss.detect_meaningful_changes(old, new)
        assert any(c["category"] == tss.GOAL_HABIT_CHANGED for c in changes)

    def test_data_quality_changed_when_summary_differs(self):
        old = tss.build_snapshot_state(_empty_state())
        new = tss.build_snapshot_state(_behavioral_state(7.0))
        changes = tss.detect_meaningful_changes(old, new)
        assert any(c["category"] == tss.DATA_QUALITY_CHANGED for c in changes)

    def test_no_medical_change_categories_exist(self):
        all_categories = {
            tss.DOMAIN_ADDED, tss.DOMAIN_REMOVED, tss.TREND_CHANGED, tss.MEMORY_CHANGED,
            tss.PATTERN_CHANGED, tss.GOAL_HABIT_CHANGED, tss.BIOMARKER_UPDATED, tss.DATA_QUALITY_CHANGED,
        }
        for forbidden in ("RISK", "DISEASE", "DIAGNOSIS", "MEDICAL"):
            assert not any(forbidden in c for c in all_categories)


class TestDecideSnapshotPersistence:
    def test_first_snapshot_for_empty_state_is_not_persisted(self):
        snapshot = tss.build_snapshot_state(_empty_state())
        should_persist, changes = tss.decide_snapshot_persistence(last_snapshot_row=None, new_snapshot_state=snapshot, today=TODAY)
        assert should_persist is False
        assert changes == []

    def test_first_meaningful_snapshot_is_persisted(self):
        snapshot = tss.build_snapshot_state(_behavioral_state(7.0))
        should_persist, changes = tss.decide_snapshot_persistence(last_snapshot_row=None, new_snapshot_state=snapshot, today=TODAY)
        assert should_persist is True
        assert changes

    def test_no_duplicate_snapshot_for_unchanged_state(self):
        snapshot = tss.build_snapshot_state(_behavioral_state(7.0))
        last_row = {"snapshot": snapshot, "created_at": "2026-08-01T09:00:00+00:00"}  # yesterday, unchanged state today
        should_persist, changes = tss.decide_snapshot_persistence(last_snapshot_row=last_row, new_snapshot_state=snapshot, today=TODAY)
        assert should_persist is False

    def test_meaningful_change_snapshot_is_persisted_even_same_day_gate_not_hit(self):
        old_snapshot = tss.build_snapshot_state(_behavioral_state(7.0))
        new_snapshot = tss.build_snapshot_state(_behavioral_state(8.0))
        last_row = {"snapshot": old_snapshot, "created_at": "2026-08-01T09:00:00+00:00"}  # different day
        should_persist, changes = tss.decide_snapshot_persistence(last_snapshot_row=last_row, new_snapshot_state=new_snapshot, today=TODAY)
        assert should_persist is True
        assert any(c["category"] == tss.TREND_CHANGED for c in changes)

    def test_second_routine_snapshot_same_day_is_blocked(self):
        old_snapshot = tss.build_snapshot_state(_behavioral_state(7.0))
        new_snapshot = tss.build_snapshot_state(_behavioral_state(8.0))
        last_row = {"snapshot": old_snapshot, "created_at": f"{TODAY.isoformat()}T09:00:00+00:00"}  # today already
        should_persist, changes = tss.decide_snapshot_persistence(last_snapshot_row=last_row, new_snapshot_state=new_snapshot, today=TODAY)
        assert should_persist is False
        assert changes  # changes are still reported even if not persisted

    def test_major_change_same_day_still_persists_a_second_checkpoint(self):
        calc_old = {"created_at": "2026-08-01T00:00:00+00:00", "biologisches_alter": 40.0, "differenz": 0.0, "marker_breakdown": []}
        calc_new = {"created_at": "2026-08-11T08:00:00+00:00", "biologisches_alter": 39.0, "differenz": -1.0, "marker_breakdown": []}
        base_kwargs = dict(profile=None, daily_entries=[], goals=[], habits=[], confirmed_memories=[], confirmed_patterns=[], google_health=None, cgm=None, nutrition=None, today=TODAY)
        old_snapshot = tss.build_snapshot_state(uts.build_unified_twin_state(biomarker_calculations=[calc_old], **base_kwargs))
        new_snapshot = tss.build_snapshot_state(uts.build_unified_twin_state(biomarker_calculations=[calc_old, calc_new], **base_kwargs))
        last_row = {"snapshot": old_snapshot, "created_at": f"{TODAY.isoformat()}T07:00:00+00:00"}
        should_persist, changes = tss.decide_snapshot_persistence(last_snapshot_row=last_row, new_snapshot_state=new_snapshot, today=TODAY)
        assert should_persist is True
        assert any(c["category"] == tss.BIOMARKER_UPDATED for c in changes)

    def test_snapshot_version_is_stamped(self):
        snapshot = tss.build_snapshot_state(_behavioral_state(7.0))
        assert snapshot["snapshot_version"] == tss.SNAPSHOT_VERSION == 1
