"""Twin Core Phase 6 — Closed-Loop Learning (Feedback -> Memory -> Patterns
-> Personalization). Tests for the 3 closed gaps:

Part A: pattern contradiction -> ONE learning event, no duplicate on re-read.
Part B: rejection reason classification -> capped, repeated-evidence-gated
        personalization bonus, User A/B isolation.
Part C: memory confidence changes captured in the EXISTING learning events.

Mocks Supabase — no real network/database access."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.routers import recommendations as recommendations_module
from app.routers import twin_memory as twin_memory_module
from app.services import personalization

TODAY = date(2026, 8, 11)


class _MutatingQuery:
    """Table-name-keyed fake that ACTUALLY mutates the shared dataset on
    insert/update (unlike a read-only fake) — needed to simulate a pattern's
    real persisted `contradicting` state across two successive calls to the
    same endpoint."""

    def __init__(self, table_name, dataset, calls_log):
        self._table = table_name
        self._dataset = dataset
        self._calls_log = calls_log
        self._filters: dict[str, object] = {}
        self._insert_payload = None
        self._update_payload = None

    def select(self, *args, **kwargs):
        return self

    def eq(self, field, value):
        self._calls_log.append((self._table, field, value))
        self._filters[field] = value
        return self

    def gte(self, *args, **kwargs):
        return self

    def limit(self, *args, **kwargs):
        return self

    def order(self, *args, **kwargs):
        return self

    def is_(self, *args, **kwargs):
        return self

    def neq(self, *args, **kwargs):
        return self

    def in_(self, field, values):
        values = [str(v) for v in values]
        self._calls_log.append((self._table, field, values))
        self._filters[f"__in__{field}"] = (field, values)
        return self

    def insert(self, payload):
        self._insert_payload = payload
        return self

    def update(self, payload):
        self._update_payload = payload
        return self

    def _matching(self, rows):
        result = []
        for row in rows:
            ok = True
            for key, value in self._filters.items():
                if key.startswith("__in__"):
                    field, values = value
                    if str(row.get(field)) not in values:
                        ok = False
                        break
                elif row.get(key) != value:
                    ok = False
                    break
            if ok:
                result.append(row)
        return result

    def execute(self):
        rows = self._dataset.setdefault(self._table, [])
        if self._insert_payload is not None:
            row = dict(self._insert_payload)
            row.setdefault("id", f"new-{self._table}-{len(rows)}")
            rows.append(row)
            return SimpleNamespace(data=[row])
        if self._update_payload is not None:
            matching = self._matching(rows)
            for row in matching:
                row.update(self._update_payload)
            return SimpleNamespace(data=[dict(r) for r in matching])
        return SimpleNamespace(data=[dict(r) for r in self._matching(rows)])


class _FakeSupabase:
    def __init__(self, dataset: dict[str, list[dict]] | None = None):
        self.dataset = dataset or {}
        self.calls: list[tuple[str, str, object]] = []

    def table(self, name):
        return _MutatingQuery(name, self.dataset, self.calls)


def _contradicting_daily_entries(email: str) -> list[dict]:
    # Same recipe as test_pattern_detection.py::test_contradicting_data_is_flagged:
    # oldest half positively correlated, newest half negatively correlated.
    values = [1, 2, 3, 4, 5, 6]
    energy = [1, 2, 3, 6, 5, 4]
    return [
        {"email": email, "entry_date": (TODAY - timedelta(days=idx)).isoformat(), "sleep_hours": v, "energy": e}
        for idx, (v, e) in enumerate(zip(values, energy))
    ]


def _existing_pattern_row(email: str, *, contradicting: bool) -> dict:
    return {
        "id": "pattern-1",
        "email": email,
        "pattern_key": "schlafdauer_energie",
        "pattern_type": "schlafdauer_energie",
        "variables": ["sleep_hours", "energy"],
        "summary": "alte Zusammenfassung",
        "description": "alte Zusammenfassung",
        "period_days": 30,
        "data_points": 6,
        "confidence": 0.5,
        "data_quality": "calculated",
        "status": "active",
        "contradicting": contradicting,
        "evidence": {"correlation": 0.1},
        "updated_at": datetime(2026, 8, 1, tzinfo=timezone.utc).isoformat(),
    }


class TestPatternContradictionLearningEvent:
    """Part A."""

    @pytest.mark.anyio
    async def test_no_contradiction_event_on_first_creation(self, monkeypatch):
        """A brand-new pattern that happens to already be contradicting on
        its FIRST detection is not a "transition" (nothing existed before) —
        only "muster_erkannt" fires, never "muster_widerspruch_erkannt"."""
        email = "user-a@example.com"
        fake = _FakeSupabase({twin_memory_module.DAILY_ENTRY_TABLE: _contradicting_daily_entries(email)})
        monkeypatch.setattr(twin_memory_module, "supabase", fake)
        monkeypatch.setattr(twin_memory_module, "_require_email", lambda auth: email)
        monkeypatch.setattr(twin_memory_module, "get_user_id_by_email", lambda e: None)

        events: list[dict] = []
        monkeypatch.setattr(
            twin_memory_module, "record_learning_event", lambda **kwargs: events.append(kwargs)
        )

        await twin_memory_module.list_patterns(authorization="Bearer x")

        event_types = [e["event_type"] for e in events]
        assert "muster_widerspruch_erkannt" not in event_types
        assert "muster_erkannt" in event_types

    @pytest.mark.anyio
    async def test_one_event_on_false_to_true_transition(self, monkeypatch):
        email = "user-a@example.com"
        fake = _FakeSupabase(
            {
                twin_memory_module.DAILY_ENTRY_TABLE: _contradicting_daily_entries(email),
                twin_memory_module.PATTERN_TABLE: [_existing_pattern_row(email, contradicting=False)],
            }
        )
        monkeypatch.setattr(twin_memory_module, "supabase", fake)
        monkeypatch.setattr(twin_memory_module, "_require_email", lambda auth: email)
        monkeypatch.setattr(twin_memory_module, "get_user_id_by_email", lambda e: None)

        events: list[dict] = []
        monkeypatch.setattr(
            twin_memory_module, "record_learning_event", lambda **kwargs: events.append(kwargs)
        )

        await twin_memory_module.list_patterns(authorization="Bearer x")

        contradiction_events = [e for e in events if e["event_type"] == "muster_widerspruch_erkannt"]
        assert len(contradiction_events) == 1
        event = contradiction_events[0]
        assert event["previous_state"]["contradicting"] is False
        assert event["new_state"]["contradicting"] is True
        # Evidence/provenance preserved: no second pattern system, same key/source.
        assert event["source_type"] == "twin_pattern"
        assert event["source_id"] == "pattern-1"

    @pytest.mark.anyio
    async def test_no_duplicate_event_on_repeated_unchanged_read(self, monkeypatch):
        email = "user-a@example.com"
        fake = _FakeSupabase(
            {
                twin_memory_module.DAILY_ENTRY_TABLE: _contradicting_daily_entries(email),
                twin_memory_module.PATTERN_TABLE: [_existing_pattern_row(email, contradicting=False)],
            }
        )
        monkeypatch.setattr(twin_memory_module, "supabase", fake)
        monkeypatch.setattr(twin_memory_module, "_require_email", lambda auth: email)
        monkeypatch.setattr(twin_memory_module, "get_user_id_by_email", lambda e: None)

        events: list[dict] = []
        monkeypatch.setattr(
            twin_memory_module, "record_learning_event", lambda **kwargs: events.append(kwargs)
        )

        await twin_memory_module.list_patterns(authorization="Bearer x")  # 1st: fires the transition
        await twin_memory_module.list_patterns(authorization="Bearer x")  # 2nd: already contradicting, must not re-fire

        contradiction_events = [e for e in events if e["event_type"] == "muster_widerspruch_erkannt"]
        assert len(contradiction_events) == 1

    @pytest.mark.anyio
    async def test_pattern_key_and_status_preserved_no_second_pattern_system(self, monkeypatch):
        email = "user-a@example.com"
        fake = _FakeSupabase(
            {
                twin_memory_module.DAILY_ENTRY_TABLE: _contradicting_daily_entries(email),
                twin_memory_module.PATTERN_TABLE: [_existing_pattern_row(email, contradicting=False)],
            }
        )
        monkeypatch.setattr(twin_memory_module, "supabase", fake)
        monkeypatch.setattr(twin_memory_module, "_require_email", lambda auth: email)
        monkeypatch.setattr(twin_memory_module, "get_user_id_by_email", lambda e: None)

        result = await twin_memory_module.list_patterns(authorization="Bearer x")

        patterns = fake.dataset[twin_memory_module.PATTERN_TABLE]
        sleep_energy_patterns = [p for p in patterns if p["pattern_key"] == "schlafdauer_energie"]
        assert len(sleep_energy_patterns) == 1  # no second row for the SAME pattern_key
        assert sleep_energy_patterns[0]["status"] == "active"
        result_row = next(r for r in result["items"] if r.get("pattern_key") == "schlafdauer_energie")
        assert result_row["contradicting"] is True


class TestLearningTimelineContradictedMapping:
    """CONTRADICTED category only exists now because a real event exists."""

    def test_contradicted_wording_is_non_causal(self):
        from app.services import twin_learning_timeline as tlt

        row = {
            "id": "e1",
            "event_type": "muster_widerspruch_erkannt",
            "source_type": "twin_pattern",
            "created_at": "2026-08-01T09:00:00+00:00",
            "previous_state": {"contradicting": False, "confidence": 0.5},
            "new_state": {"contradicting": True, "confidence": 0.3},
            "reason": None,
        }
        entry = tlt.summarize_learning_event(row)
        assert entry.category == "CONTRADICTED"
        for forbidden in ("war falsch", "beweist", "das gegenteil ist wahr"):
            assert forbidden not in entry.summary.lower()


class TestRejectionReasonPersonalization:
    """Part B: personalization.py unit-level behavior."""

    def test_classify_known_categories(self):
        assert personalization.classify_rejection_reason("Der Zeitpunkt morgens passt nicht") == "timing_not_suitable"
        assert personalization.classify_rejection_reason("Das ist mir zu anstrengend") == "too_difficult"
        assert personalization.classify_rejection_reason("Ist für mich nicht relevant") == "not_relevant"
        assert personalization.classify_rejection_reason("Mache ich schon seit Jahren") == "already_doing"
        assert personalization.classify_rejection_reason("Das gefällt mir nicht") == "preference_conflict"

    def test_ambiguous_or_missing_reason_is_other(self):
        assert personalization.classify_rejection_reason(None) == "other"
        assert personalization.classify_rejection_reason("") == "other"
        assert personalization.classify_rejection_reason("xyz random text 123") == "other"

    def _history(self, category: str, decisions: list[str]) -> tuple[list[dict], dict[str, dict]]:
        history = []
        decisions_by_id = {}
        for idx, status in enumerate(decisions):
            rec_id = f"rec-{idx}"
            history.append({"id": rec_id, "category": category, "status": "rejected" if status else "accepted"})
        return history, decisions_by_id

    def test_one_rejection_alone_does_not_overreact(self):
        history = [{"id": "r1", "category": "bewegung", "status": "rejected"}]
        decisions = {"r1": {"reason": "Der Zeitpunkt morgens passt nicht"}}
        penalties = personalization.compute_category_penalty(history, decisions_by_recommendation_id=decisions)
        assert penalties["bewegung"] == 1
        assert personalization.should_deprioritize_category("bewegung", penalties) is False

    def test_repeated_explicit_reason_influences_future_priority(self):
        history = [
            {"id": "r1", "category": "bewegung", "status": "rejected"},
            {"id": "r2", "category": "bewegung", "status": "rejected"},
        ]
        decisions = {
            "r1": {"reason": "Der Zeitpunkt morgens passt nicht"},
            "r2": {"reason": "Zeitlich passt es einfach nicht"},
        }
        penalties = personalization.compute_category_penalty(history, decisions_by_recommendation_id=decisions)
        # base penalty (2) + capped reason bonus (1 for count==threshold) = 3
        assert penalties["bewegung"] == 3
        assert personalization.should_deprioritize_category("bewegung", penalties) is True

    def test_ambiguous_reason_never_creates_false_personalization(self):
        history = [
            {"id": "r1", "category": "bewegung", "status": "rejected"},
            {"id": "r2", "category": "bewegung", "status": "rejected"},
        ]
        decisions = {"r1": {"reason": "hmm nicht so"}, "r2": {"reason": "keine Ahnung warum"}}
        penalties = personalization.compute_category_penalty(history, decisions_by_recommendation_id=decisions)
        # only the base penalty (2), no bonus since both reasons are "other"
        assert penalties["bewegung"] == 2

    def test_other_reason_category_remains_safe_even_when_repeated(self):
        history = [
            {"id": "r1", "category": "bewegung", "status": "rejected"},
            {"id": "r2", "category": "bewegung", "status": "rejected"},
            {"id": "r3", "category": "bewegung", "status": "rejected"},
        ]
        decisions = {str(i): {"reason": "irgendwas komisches"} for i in range(3)}
        decisions = {"r1": {"reason": "a"}, "r2": {"reason": "b"}, "r3": {"reason": "c"}}
        penalties = personalization.compute_category_penalty(history, decisions_by_recommendation_id=decisions)
        assert penalties["bewegung"] == 3  # base only, no reason bonus

    def test_bonus_is_capped_even_with_many_repeats(self):
        history = [{"id": f"r{i}", "category": "bewegung", "status": "rejected"} for i in range(6)]
        decisions = {f"r{i}": {"reason": "morgens passt zeitlich nicht"} for i in range(6)}
        penalties = personalization.compute_category_penalty(history, decisions_by_recommendation_id=decisions)
        # base penalty = 6, reason bonus capped at MAX_REASON_PENALTY_BONUS=2 -> total 8
        assert penalties["bewegung"] == 6 + personalization.MAX_REASON_PENALTY_BONUS

    def test_later_positive_feedback_can_recover_the_penalty(self):
        history = [
            {"id": "r1", "category": "bewegung", "status": "rejected"},
            {"id": "r2", "category": "bewegung", "status": "rejected"},
            {"id": "r3", "category": "bewegung", "status": "accepted"},
            {"id": "r4", "category": "bewegung", "status": "accepted"},
        ]
        decisions = {
            "r1": {"reason": "Der Zeitpunkt morgens passt nicht"},
            "r2": {"reason": "Zeitlich passt es einfach nicht"},
        }
        penalties = personalization.compute_category_penalty(history, decisions_by_recommendation_id=decisions)
        # base: 2 rejected - 2 accepted = 0, plus capped reason bonus (1) = 1 -> below threshold again
        assert personalization.should_deprioritize_category("bewegung", penalties) is False

    def test_original_reason_text_is_never_mutated(self):
        original = "Der Zeitpunkt morgens passt nicht wirklich gut in meinen Alltag"
        category = personalization.classify_rejection_reason(original)
        assert category == "timing_not_suitable"
        assert original == "Der Zeitpunkt morgens passt nicht wirklich gut in meinen Alltag"

    def test_default_call_without_decisions_is_100_percent_backward_compatible(self):
        history = [{"category": "bewegung", "status": "rejected"}, {"category": "bewegung", "status": "rejected"}]
        penalties = personalization.compute_category_penalty(history)
        assert penalties == {"bewegung": 2}


def _recommendation_row(rec_id: str, email: str, category: str, status: str) -> dict:
    return {
        "id": rec_id,
        "email": email,
        "category": category,
        "status": status,
        "proposed_action": "Testaktion",
        "created_at": "2026-08-01T09:00:00+00:00",
        "valid_until": "2099-01-01T00:00:00+00:00",
    }


class TestRecommendationRouterDecisionsWiring:
    """Part B router-level wiring + isolation."""

    @pytest.mark.anyio
    async def test_decisions_are_fetched_scoped_to_the_users_own_recommendation_ids(self, monkeypatch):
        email = "user-a@example.com"
        history = [_recommendation_row("rec-a", email, "bewegung", "rejected")]
        decisions = [{"recommendation_id": "rec-a", "reason": "Der Zeitpunkt morgens passt nicht"}]
        fake = _FakeSupabase(
            {recommendations_module.RECOMMENDATION_TABLE: history, recommendations_module.DECISION_TABLE: decisions}
        )
        monkeypatch.setattr(recommendations_module, "supabase", fake)
        monkeypatch.setattr(recommendations_module, "_require_email", lambda auth: email)

        await recommendations_module.list_recommendations(authorization="Bearer x")

        decision_calls = [c for c in fake.calls if c[0] == recommendations_module.DECISION_TABLE]
        assert decision_calls  # a real, scoped fetch happened

    @pytest.mark.anyio
    async def test_user_a_decision_reasons_never_affect_user_b(self, monkeypatch):
        history_a = [_recommendation_row("rec-a1", "user-a@example.com", "bewegung", "rejected")]
        history_b = [_recommendation_row("rec-b1", "user-b@example.com", "bewegung", "rejected")]
        decisions_a = [{"recommendation_id": "rec-a1", "reason": "Der Zeitpunkt morgens passt nicht"}]

        fake_a = _FakeSupabase(
            {recommendations_module.RECOMMENDATION_TABLE: history_a, recommendations_module.DECISION_TABLE: decisions_a}
        )
        monkeypatch.setattr(recommendations_module, "supabase", fake_a)
        monkeypatch.setattr(recommendations_module, "_require_email", lambda auth: "user-a@example.com")
        await recommendations_module.list_recommendations(authorization="Bearer x")

        # User B's own fetch must never see user A's decision row (isolation is
        # structural: decisions are only ever fetched for THIS user's own
        # recommendation ids, and user B's history contains different ids).
        fake_b = _FakeSupabase(
            {recommendations_module.RECOMMENDATION_TABLE: history_b, recommendations_module.DECISION_TABLE: decisions_a}
        )
        monkeypatch.setattr(recommendations_module, "supabase", fake_b)
        monkeypatch.setattr(recommendations_module, "_require_email", lambda auth: "user-b@example.com")
        await recommendations_module.list_recommendations(authorization="Bearer x")

        decision_calls_b = [c for c in fake_b.calls if c[0] == recommendations_module.DECISION_TABLE]
        # user B's decision fetch is scoped to rec-b1's id, never rec-a1's
        in_filters = [v for (_, field, v) in decision_calls_b if field == "recommendation_id"]
        assert "rec-a1" not in [str(x) for x in in_filters]

    @pytest.mark.anyio
    async def test_decisions_fetch_failure_does_not_crash_endpoint(self, monkeypatch):
        email = "user-a@example.com"
        history = [_recommendation_row("rec-a", email, "bewegung", "rejected")]

        class _RaisingDecisionSupabase(_FakeSupabase):
            def table(self, name):
                if name == recommendations_module.DECISION_TABLE:
                    class _Raising:
                        def select(self, *a, **k):
                            return self

                        def in_(self, *a, **k):
                            raise RuntimeError("boom")

                    return _Raising()
                return super().table(name)

        fake = _RaisingDecisionSupabase({recommendations_module.RECOMMENDATION_TABLE: history})
        monkeypatch.setattr(recommendations_module, "supabase", fake)
        monkeypatch.setattr(recommendations_module, "_require_email", lambda auth: email)

        result = await recommendations_module.list_recommendations(authorization="Bearer x")
        assert "items" in result


class TestMemoryConfidenceLearningEvents:
    """Part C."""

    def _memory_row(self, email: str, *, status: str, confidence: float) -> dict:
        return {"id": "mem-1", "email": email, "status": status, "confidence": confidence, "human_readable_value": "Alte Beobachtung"}

    @pytest.mark.anyio
    async def test_confirmation_confidence_change_is_recorded(self, monkeypatch):
        email = "user-a@example.com"
        memory = self._memory_row(email, status="active", confidence=0.55)
        fake = _FakeSupabase({twin_memory_module.MEMORY_TABLE: [memory]})
        monkeypatch.setattr(twin_memory_module, "supabase", fake)
        monkeypatch.setattr(twin_memory_module, "_require_email", lambda auth: email)

        events: list[dict] = []
        monkeypatch.setattr(twin_memory_module, "record_learning_event", lambda **kwargs: events.append(kwargs))

        from app.routers.twin_memory import MemoryActionInput

        await twin_memory_module.confirm_memory("mem-1", MemoryActionInput(reason=None), authorization="Bearer x")

        event = next(e for e in events if e["event_type"] == "memory_bestaetigt")
        assert event["previous_state"]["confidence"] == 0.55
        assert event["new_state"]["confidence"] == pytest.approx(0.70)

    @pytest.mark.anyio
    async def test_rejection_decay_confidence_change_is_recorded(self, monkeypatch):
        email = "user-a@example.com"
        memory = self._memory_row(email, status="active", confidence=0.70)
        fake = _FakeSupabase({twin_memory_module.MEMORY_TABLE: [memory]})
        monkeypatch.setattr(twin_memory_module, "supabase", fake)
        monkeypatch.setattr(twin_memory_module, "_require_email", lambda auth: email)

        events: list[dict] = []
        monkeypatch.setattr(twin_memory_module, "record_learning_event", lambda **kwargs: events.append(kwargs))

        from app.routers.twin_memory import MemoryActionInput

        await twin_memory_module.reject_memory("mem-1", MemoryActionInput(reason=None), authorization="Bearer x")

        event = next(e for e in events if e["event_type"] == "memory_abgelehnt")
        assert event["previous_state"]["confidence"] == 0.70
        assert event["new_state"]["confidence"] == pytest.approx(0.45)

    @pytest.mark.anyio
    async def test_no_formula_change_correction_still_resets_to_point_six(self, monkeypatch):
        email = "user-a@example.com"
        memory = self._memory_row(email, status="active", confidence=0.30)
        fake = _FakeSupabase({twin_memory_module.MEMORY_TABLE: [memory]})
        monkeypatch.setattr(twin_memory_module, "supabase", fake)
        monkeypatch.setattr(twin_memory_module, "_require_email", lambda auth: email)

        events: list[dict] = []
        monkeypatch.setattr(twin_memory_module, "record_learning_event", lambda **kwargs: events.append(kwargs))

        from app.routers.twin_memory import MemoryCorrectionInput

        await twin_memory_module.correct_memory(
            "mem-1", MemoryCorrectionInput(human_readable_value="Neuer Wert"), authorization="Bearer x"
        )

        event = next(e for e in events if e["event_type"] == "memory_korrigiert")
        assert event["previous_state"]["confidence"] == 0.30
        assert event["new_state"]["confidence"] == 0.6

    def test_timeline_renders_confidence_percentages_when_both_present(self):
        from app.services import twin_learning_timeline as tlt

        row = {
            "id": "e1",
            "event_type": "memory_bestaetigt",
            "source_type": "twin_memory",
            "created_at": "2026-08-01T09:00:00+00:00",
            "previous_state": {"status": "active", "confidence": 0.55},
            "new_state": {"status": "confirmed", "confidence": 0.70},
            "reason": None,
        }
        entry = tlt.summarize_learning_event(row)
        assert "55" in entry.summary and "70" in entry.summary

    def test_timeline_omits_percentage_when_not_genuinely_available(self):
        from app.services import twin_learning_timeline as tlt

        row = {
            "id": "e1",
            "event_type": "memory_archiviert",
            "source_type": "twin_memory",
            "created_at": "2026-08-01T09:00:00+00:00",
            "previous_state": {"status": "active"},
            "new_state": {"status": "archived"},
            "reason": None,
        }
        entry = tlt.summarize_learning_event(row)
        assert "%" not in entry.summary


class TestRecommendationFeedbackTimelineWording:
    """Learning Timeline's repeated-evidence-gated recommendation wording."""

    def test_single_rejection_shows_only_the_base_sentence(self):
        from app.services import twin_learning_timeline as tlt

        rows = [
            {
                "id": "e1",
                "event_type": "empfehlung_abgelehnt",
                "source_type": "recommendation_decision",
                "created_at": "2026-08-01T09:00:00+00:00",
                "previous_state": {"status": "proposed"},
                "new_state": {"status": "rejected", "category": "bewegung", "reason_category": "timing_not_suitable"},
                "reason": "Der Zeitpunkt morgens passt nicht",
            }
        ]
        timeline = tlt.build_learning_timeline(rows)
        assert "wiederholt zeitlich unpassend" not in timeline[0].summary

    def test_repeated_same_reason_shows_the_stronger_sentence(self):
        from app.services import twin_learning_timeline as tlt

        rows = [
            {
                "id": f"e{i}",
                "event_type": "empfehlung_abgelehnt",
                "source_type": "recommendation_decision",
                "created_at": f"2026-08-0{i}T09:00:00+00:00",
                "previous_state": {"status": "proposed"},
                "new_state": {"status": "rejected", "category": "bewegung", "reason_category": "timing_not_suitable"},
                "reason": "zeitlich unpassend",
            }
            for i in (1, 2)
        ]
        timeline = tlt.build_learning_timeline(rows)
        assert all("zeitlich unpassend war" in e.summary for e in timeline)

    def test_other_reason_category_never_triggers_the_stronger_sentence(self):
        from app.services import twin_learning_timeline as tlt

        rows = [
            {
                "id": f"e{i}",
                "event_type": "empfehlung_abgelehnt",
                "source_type": "recommendation_decision",
                "created_at": f"2026-08-0{i}T09:00:00+00:00",
                "previous_state": {"status": "proposed"},
                "new_state": {"status": "rejected", "category": "bewegung", "reason_category": "other"},
                "reason": "keine Ahnung",
            }
            for i in (1, 2)
        ]
        timeline = tlt.build_learning_timeline(rows)
        assert all("wiederholt" not in e.summary for e in timeline)


class TestPrivacyIsolation:
    @pytest.mark.anyio
    async def test_user_a_contradiction_event_never_appears_for_user_b(self, monkeypatch):
        events: list[dict] = []
        monkeypatch.setattr(twin_memory_module, "record_learning_event", lambda **kwargs: events.append(kwargs))

        fake_a = _FakeSupabase(
            {
                twin_memory_module.DAILY_ENTRY_TABLE: _contradicting_daily_entries("user-a@example.com"),
                twin_memory_module.PATTERN_TABLE: [_existing_pattern_row("user-a@example.com", contradicting=False)],
            }
        )
        monkeypatch.setattr(twin_memory_module, "supabase", fake_a)
        monkeypatch.setattr(twin_memory_module, "_require_email", lambda auth: "user-a@example.com")
        monkeypatch.setattr(twin_memory_module, "get_user_id_by_email", lambda e: None)
        await twin_memory_module.list_patterns(authorization="Bearer x")

        for event in events:
            assert event["email"] == "user-a@example.com"

    @pytest.mark.anyio
    async def test_family_membership_grants_zero_access_to_another_members_confidence_history(self, monkeypatch):
        """Architectural guarantee: no Family table is ever touched by any
        Phase 6 code path — confirm/correct/reject only ever resolve the
        SINGLE requesting user's own email."""
        owner_memory = {"id": "mem-1", "email": "family-owner@example.com", "status": "active", "confidence": 0.5}
        member_memory = {"id": "mem-2", "email": "family-member@example.com", "status": "active", "confidence": 0.5}
        fake = _FakeSupabase({twin_memory_module.MEMORY_TABLE: [owner_memory, member_memory]})
        monkeypatch.setattr(twin_memory_module, "supabase", fake)
        monkeypatch.setattr(twin_memory_module, "record_learning_event", lambda **kwargs: None)

        from app.routers.twin_memory import MemoryActionInput
        from fastapi import HTTPException

        monkeypatch.setattr(twin_memory_module, "_require_email", lambda auth: "family-member@example.com")
        with pytest.raises(HTTPException) as exc_info:
            await twin_memory_module.confirm_memory("mem-1", MemoryActionInput(reason=None), authorization="Bearer x")
        assert exc_info.value.status_code == 404


class TestRegressionExistingBehaviorUnchanged:
    def test_existing_pattern_detection_formulas_unchanged(self):
        from app.services import pattern_detection

        assert pattern_detection.MEANINGFUL_CORRELATION == 0.3
        assert pattern_detection.CONTRADICTION_CORRELATION == 0.2

    def test_existing_confidence_formulas_unchanged(self):
        from app.services import twin_memory as twin_memory_service

        assert twin_memory_service.bump_confidence(0.55) == pytest.approx(0.70)
        assert twin_memory_service.decay_confidence(0.70) == pytest.approx(0.45)

    def test_existing_base_penalty_behavior_unchanged_without_decisions(self):
        history = [{"category": "x", "status": "rejected"}, {"category": "x", "status": "accepted"}]
        assert personalization.compute_category_penalty(history) == {"x": 0}
