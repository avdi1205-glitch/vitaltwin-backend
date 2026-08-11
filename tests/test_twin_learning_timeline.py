"""Unit tests for `app.services.twin_learning_timeline`. Pure functions over
already-fetched rows, no database access."""

from __future__ import annotations

from app.services import twin_learning_timeline as tlt


def _row(event_type, source_type, *, created_at="2026-08-01T10:00:00+00:00", **kwargs) -> dict:
    row = {
        "id": kwargs.pop("id", "evt-1"),
        "event_type": event_type,
        "source_type": source_type,
        "created_at": created_at,
        "previous_state": kwargs.pop("previous_state", None),
        "new_state": kwargs.pop("new_state", {}),
        "reason": kwargs.pop("reason", None),
        "source_id": kwargs.pop("source_id", "mem-1"),
    }
    row.update(kwargs)
    return row


class TestSummarizeLearningEvent:
    def test_unrecognized_event_type_is_filtered_out(self):
        row = _row("some_future_internal_event", "twin_memory")
        assert tlt.summarize_learning_event(row) is None

    def test_legacy_ambiguous_muster_erkannt_without_resolvable_source_type_is_filtered_out(self):
        # "muster_erkannt" needs source_type to resolve memory-vs-pattern domain;
        # a legacy/malformed row missing it must not be guessed.
        row = _row("muster_erkannt", None)
        assert tlt.summarize_learning_event(row) is None

    def test_praeferenz_erkannt_maps_to_learned(self):
        row = _row("praeferenz_erkannt", "twin_memory", reason="Bewegung am Vormittag")
        entry = tlt.summarize_learning_event(row)
        assert entry.category == "LEARNED"
        assert entry.related_domain == "memory"
        assert "Bewegung am Vormittag" in entry.summary

    def test_muster_erkannt_resolves_domain_via_source_type_memory(self):
        row = _row("muster_erkannt", "twin_memory")
        entry = tlt.summarize_learning_event(row)
        assert entry.category == "LEARNED"
        assert entry.related_domain == "memory"

    def test_muster_erkannt_resolves_domain_via_source_type_pattern(self):
        row = _row("muster_erkannt", "twin_pattern", new_state={"pattern_type": "x", "confidence": 0.42})
        entry = tlt.summarize_learning_event(row)
        assert entry.category == "LEARNED"
        assert entry.related_domain == "pattern"
        assert entry.confidence_after == 0.42

    def test_memory_erstellt_auto_observation_wording(self):
        row = _row("memory_erstellt", "twin_memory", new_state={"status": "candidate"}, reason="Schläfst meist 7h")
        entry = tlt.summarize_learning_event(row)
        assert entry.category == "LEARNED"
        assert "Schläfst meist 7h" in entry.summary

    def test_memory_erstellt_manual_statement_wording(self):
        row = _row("memory_erstellt", "twin_memory", new_state={"status": "confirmed"}, reason="Ich mag keine Milchprodukte")
        entry = tlt.summarize_learning_event(row)
        assert "mitgeteilt" in entry.summary

    def test_praeferenz_bestaetigt_maps_to_confirmed(self):
        row = _row("praeferenz_bestaetigt", "twin_memory", reason="Wiederholt beobachtet")
        entry = tlt.summarize_learning_event(row)
        assert entry.category == "CONFIRMED"

    def test_memory_bestaetigt_maps_to_confirmed(self):
        row = _row("memory_bestaetigt", "twin_memory")
        entry = tlt.summarize_learning_event(row)
        assert entry.category == "CONFIRMED"

    def test_memory_korrigiert_maps_to_corrected_by_user_with_new_value(self):
        row = _row("memory_korrigiert", "twin_memory", new_state={"human_readable_value": "Ich trainiere abends"})
        entry = tlt.summarize_learning_event(row)
        assert entry.category == "CORRECTED_BY_USER"
        assert "Ich trainiere abends" in entry.summary

    def test_memory_abgelehnt_archiviert_geloescht_map_to_discarded(self):
        for event_type in ("memory_abgelehnt", "memory_archiviert", "memory_geloescht"):
            row = _row(event_type, "twin_memory")
            entry = tlt.summarize_learning_event(row)
            assert entry.category == "DISCARDED", event_type

    def test_muster_verworfen_maps_to_discarded_pattern(self):
        row = _row("muster_verworfen", "twin_pattern")
        entry = tlt.summarize_learning_event(row)
        assert entry.category == "DISCARDED"
        assert entry.related_domain == "pattern"

    def test_ziel_angepasst_maps_to_updated(self):
        row = _row("ziel_angepasst", "wellness_goal", new_state={"status": "completed"})
        entry = tlt.summarize_learning_event(row)
        assert entry.category == "UPDATED"
        assert entry.related_domain == "goal"
        assert "completed" in entry.summary

    def test_empfehlung_abgelehnt_maps_to_feedback_adaptation(self):
        row = _row("empfehlung_abgelehnt", "recommendation_decision", reason="zu anstrengend")
        entry = tlt.summarize_learning_event(row)
        assert entry.category == "FEEDBACK_ADAPTATION"
        assert entry.related_domain == "recommendation"
        assert "zu anstrengend" in entry.summary

    def test_empfehlung_erfolgreich_maps_to_feedback_adaptation(self):
        row = _row("empfehlung_erfolgreich", "recommendation_outcome")
        entry = tlt.summarize_learning_event(row)
        assert entry.category == "FEEDBACK_ADAPTATION"

    def test_missing_reason_never_fabricates_a_reason_clause(self):
        row = _row("memory_abgelehnt", "twin_memory", reason=None)
        entry = tlt.summarize_learning_event(row)
        assert entry.summary == "Du hast diese Beobachtung abgelehnt."

    def test_confidence_only_present_when_genuinely_stored(self):
        row = _row("memory_bestaetigt", "twin_memory", previous_state={"status": "candidate"}, new_state={"status": "confirmed"})
        entry = tlt.summarize_learning_event(row)
        assert entry.confidence_before is None
        assert entry.confidence_after is None

    def test_muster_widerspruch_erkannt_maps_to_contradicted(self):
        # Twin Core Phase 6 Part A: CONTRADICTED now has a real backing event.
        row = _row("muster_widerspruch_erkannt", "twin_pattern")
        entry = tlt.summarize_learning_event(row)
        assert entry.category == "CONTRADICTED"
        assert entry.related_domain == "pattern"
        assert "nicht mehr" in entry.summary
        assert "beweist" not in entry.summary.lower()
        assert "falsch" not in entry.summary.lower()


class TestBuildLearningTimeline:
    def test_empty_rows_returns_empty_timeline(self):
        assert tlt.build_learning_timeline([]) == []

    def test_newest_first_ordering(self):
        rows = [
            _row("memory_bestaetigt", "twin_memory", created_at="2026-08-01T09:00:00+00:00", id="a"),
            _row("memory_bestaetigt", "twin_memory", created_at="2026-08-03T09:00:00+00:00", id="b"),
            _row("memory_bestaetigt", "twin_memory", created_at="2026-08-02T09:00:00+00:00", id="c"),
        ]
        timeline = tlt.build_learning_timeline(rows)
        assert [e.id for e in timeline] == ["b", "c", "a"]

    def test_unrecognized_events_are_filtered_from_timeline(self):
        rows = [
            _row("memory_bestaetigt", "twin_memory", id="a"),
            _row("some_internal_junk_event", "twin_memory", id="junk"),
        ]
        timeline = tlt.build_learning_timeline(rows)
        assert [e.id for e in timeline] == ["a"]

    def test_pattern_and_memory_events_both_included_correctly_typed(self):
        rows = [
            _row("muster_erkannt", "twin_pattern", id="p1"),
            _row("praeferenz_erkannt", "twin_memory", id="m1"),
        ]
        timeline = tlt.build_learning_timeline(rows)
        domains = {e.id: e.related_domain for e in timeline}
        assert domains == {"p1": "pattern", "m1": "memory"}

    def test_recommendation_feedback_events_included(self):
        rows = [_row("empfehlung_abgelehnt", "recommendation_decision", id="r1", source_id="rec-1")]
        timeline = tlt.build_learning_timeline(rows)
        assert timeline[0].category == "FEEDBACK_ADAPTATION"

    def test_missing_metadata_handled_without_crash(self):
        rows = [{"id": "x", "event_type": "memory_bestaetigt", "source_type": "twin_memory", "created_at": None}]
        timeline = tlt.build_learning_timeline(rows)
        assert len(timeline) == 1
        assert timeline[0].occurred_at is None

    def test_current_state_enrichment_marks_still_current_memory(self):
        rows = [_row("memory_bestaetigt", "twin_memory", id="m1", source_id="mem-42")]
        enrichment = {"memory": {"mem-42": {"status": "confirmed"}}}
        timeline = tlt.build_learning_timeline(rows, source_ids_by_domain=enrichment)
        assert timeline[0].current_status == "confirmed"
        assert timeline[0].is_current is True

    def test_current_state_enrichment_marks_no_longer_current(self):
        rows = [_row("memory_bestaetigt", "twin_memory", id="m1", source_id="mem-42")]
        enrichment = {"memory": {"mem-42": {"status": "archived"}}}
        timeline = tlt.build_learning_timeline(rows, source_ids_by_domain=enrichment)
        assert timeline[0].current_status == "archived"
        assert timeline[0].is_current is False

    def test_enrichment_never_rewrites_the_historical_summary(self):
        rows = [_row("memory_korrigiert", "twin_memory", id="m1", source_id="mem-42", new_state={"human_readable_value": "Alter Wert zum Zeitpunkt des Events"})]
        enrichment = {"memory": {"mem-42": {"status": "confirmed", "human_readable_value": "Ganz neuer aktueller Wert"}}}
        timeline = tlt.build_learning_timeline(rows, source_ids_by_domain=enrichment)
        assert "Alter Wert zum Zeitpunkt des Events" in timeline[0].summary
        assert "Ganz neuer aktueller Wert" not in timeline[0].summary

    def test_enrichment_missing_entity_leaves_current_state_none(self):
        rows = [_row("memory_bestaetigt", "twin_memory", id="m1", source_id="mem-does-not-exist-anymore")]
        enrichment = {"memory": {}}
        timeline = tlt.build_learning_timeline(rows, source_ids_by_domain=enrichment)
        assert timeline[0].current_status is None
        assert timeline[0].is_current is None

    def test_no_enrichment_map_leaves_current_state_none(self):
        rows = [_row("memory_bestaetigt", "twin_memory", id="m1")]
        timeline = tlt.build_learning_timeline(rows)
        assert timeline[0].current_status is None
        assert timeline[0].is_current is None
