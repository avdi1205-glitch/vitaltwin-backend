"""Unit tests for `app.routers.chat` — Pydantic validation, plan resolution,
the provider factory, and (critically) that every context query is scoped
to the requesting user's own email (Etappe 7 §1: "nur aktueller Nutzer",
"keine fremden Daten"). Mocks the Supabase client — no real network/database
access."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.routers import chat as chat_module
from app.routers.chat import ChatRequest
from app.services.ai_provider import OpenAIProvider


class _RecordingQuery:
    def __init__(self, table_name: str, calls_log: list[tuple[str, str, object]]):
        self._table_name = table_name
        self._calls_log = calls_log

    def select(self, *args, **kwargs):
        return self

    def eq(self, field, value):
        self._calls_log.append((self._table_name, field, value))
        return self

    def neq(self, *args, **kwargs):
        return self

    def in_(self, *args, **kwargs):
        return self

    def is_(self, *args, **kwargs):
        return self

    def order(self, *args, **kwargs):
        return self

    def limit(self, *args, **kwargs):
        return self

    def gte(self, field, value):
        self._calls_log.append((self._table_name, field, value))
        return self

    def execute(self):
        return SimpleNamespace(data=[])


class _RecordingSupabase:
    def __init__(self):
        self.calls: list[tuple[str, str, object]] = []

    def table(self, name):
        return _RecordingQuery(name, self.calls)


class TestChatRequestValidation:
    def test_accepts_a_normal_message(self):
        assert ChatRequest(message="Wie war meine Woche?").message == "Wie war meine Woche?"

    def test_rejects_empty_message(self):
        with pytest.raises(ValidationError):
            ChatRequest(message="   ")

    def test_rejects_overlong_message(self):
        with pytest.raises(ValidationError):
            ChatRequest(message="x" * 501)


class TestCurrentPlan:
    def test_premium_user_resolves_to_premium(self, monkeypatch):
        monkeypatch.setattr(chat_module, "get_plan_by_email", lambda email: "premium")
        assert chat_module._current_plan("user@example.com") == "premium"

    def test_pro_user_resolves_to_pro(self, monkeypatch):
        monkeypatch.setattr(chat_module, "get_plan_by_email", lambda email: "pro")
        assert chat_module._current_plan("user@example.com") == "pro"

    def test_non_premium_user_resolves_to_free(self, monkeypatch):
        monkeypatch.setattr(chat_module, "get_plan_by_email", lambda email: "free")
        assert chat_module._current_plan("user@example.com") == "free"


class TestAIProviderFactory:
    def test_factory_returns_an_openai_provider_instance(self):
        assert isinstance(chat_module._get_ai_provider(), OpenAIProvider)


class TestContextQueriesAreScopedToRequestingUser:
    """Etappe 7 §1: "nur aktueller Nutzer", "keine fremden Daten". Every
    table this function touches must filter by the requesting user's own
    email — never by anything client-supplied, never unscoped."""

    EXPECTED_EMAIL_SCOPED_TABLES = {
        chat_module.PROFILE_TABLE,
        chat_module.GOAL_TABLE,
        chat_module.HABIT_TABLE,
        chat_module.HABIT_ENTRY_TABLE,
        chat_module.DAILY_ENTRY_TABLE,
        chat_module.MEMORY_TABLE,
        chat_module.RECOMMENDATION_TABLE,
        chat_module.PATTERN_TABLE,
        chat_module.DAILY_PLAN_TABLE,
        chat_module.BIOMARKER_CALC_TABLE,
    }

    def test_every_expected_table_is_filtered_by_the_requesting_email(self, monkeypatch):
        fake_supabase = _RecordingSupabase()
        monkeypatch.setattr(chat_module, "supabase", fake_supabase)

        email = "user-a@example.com"
        chat_module._build_context_for_user(email, "free")

        email_scoped_tables = {table for table, field, value in fake_supabase.calls if field == "email" and value == email}
        assert self.EXPECTED_EMAIL_SCOPED_TABLES.issubset(email_scoped_tables)

    def test_no_call_ever_uses_a_different_email(self, monkeypatch):
        fake_supabase = _RecordingSupabase()
        monkeypatch.setattr(chat_module, "supabase", fake_supabase)

        email = "user-a@example.com"
        chat_module._build_context_for_user(email, "free")

        foreign_email_calls = [call for call in fake_supabase.calls if call[1] == "email" and call[2] != email]
        assert foreign_email_calls == []

    def test_returns_empty_context_without_raising_when_supabase_has_no_data(self, monkeypatch):
        fake_supabase = _RecordingSupabase()
        monkeypatch.setattr(chat_module, "supabase", fake_supabase)

        text, sources, truncated, profile = chat_module._build_context_for_user("user-a@example.com", "free")
        assert isinstance(text, str) and text
        assert sources == []
        assert truncated is False
        assert profile is None


def _old_language_resolution(profile_rows):
    """Verbatim reconstruction of the OLD `ask_twin()` snippet removed by the
    profile-reuse optimization (`profile_resp.data[0].get(...) if data else
    None) or "de"`, wrapped in the same try/except("de")). Used only to
    prove the NEW logic resolves to the identical language for every state."""
    try:
        return (profile_rows[0].get("preferred_language") if profile_rows else None) or "de"
    except Exception:
        return "de"


class TestLanguageResolutionOldVsNewEquivalence:
    """Proves (not just reasons) that reusing `_build_context_for_user`'s
    already-fetched profile row resolves `preferred_language` identically
    to the old, separate query for every possible profile state."""

    @pytest.mark.parametrize(
        "profile_rows,expected_language",
        [
            ([{"preferred_language": "de"}], "de"),
            ([{"preferred_language": "en"}], "en"),
            ([{"email": "user-a@example.com"}], "de"),  # preferred_language key missing
            ([], "de"),  # query ran, zero rows
            (None, "de"),  # data attribute absent / falsy
        ],
    )
    def test_new_extraction_matches_old_extraction(self, profile_rows, expected_language):
        old_result = _old_language_resolution(profile_rows)
        assert old_result == expected_language

        # NEW logic: `profile` is the dict `_build_context_for_user` already
        # returns (first row or None) — same extraction, different source.
        profile = profile_rows[0] if profile_rows else None
        new_result = (profile.get("preferred_language") if profile else None) or "de"
        assert new_result == old_result

    def test_missing_profile_row_resolves_to_de_via_real_build_context_for_user(self, monkeypatch):
        fake_supabase = _RecordingSupabase()
        monkeypatch.setattr(chat_module, "supabase", fake_supabase)

        _, _, _, profile = chat_module._build_context_for_user("user-a@example.com", "free")
        assert profile is None
        assert ((profile.get("preferred_language") if profile else None) or "de") == "de"

    def test_db_error_during_profile_fetch_resolves_to_de_via_real_build_context_for_user(self, monkeypatch):
        class _RaisingProfileQuery(_RecordingQuery):
            def execute(self):
                raise RuntimeError("simulated database error")

        class _PartlyRaisingSupabase(_RecordingSupabase):
            def table(self, name):
                if name == chat_module.PROFILE_TABLE:
                    return _RaisingProfileQuery(name, self.calls)
                return super().table(name)

        monkeypatch.setattr(chat_module, "supabase", _PartlyRaisingSupabase())
        _, _, _, profile = chat_module._build_context_for_user("user-a@example.com", "free")
        assert profile is None
        assert ((profile.get("preferred_language") if profile else None) or "de") == "de"

    def test_profile_query_is_isolated_to_the_requesting_users_own_email(self, monkeypatch):
        fake_supabase = _RecordingSupabase()
        monkeypatch.setattr(chat_module, "supabase", fake_supabase)

        chat_module._build_context_for_user("user-a@example.com", "free")

        profile_calls = [c for c in fake_supabase.calls if c[0] == chat_module.PROFILE_TABLE]
        assert profile_calls == [(chat_module.PROFILE_TABLE, "email", "user-a@example.com")]


HEALTH_TABLES = {chat_module.HEALTH_ACTIVITY_TABLE, chat_module.HEALTH_SLEEP_TABLE, chat_module.HEALTH_METRIC_TABLE}


class _HealthAwareQuery(_RecordingQuery):
    def __init__(self, table_name, calls_log, data_by_user_id):
        super().__init__(table_name, calls_log)
        self._data_by_user_id = data_by_user_id
        self._filtered_user_id = None

    def eq(self, field, value):
        super().eq(field, value)
        if field == "user_id":
            self._filtered_user_id = value
        return self

    def execute(self):
        return SimpleNamespace(data=list(self._data_by_user_id.get(self._filtered_user_id, [])))


class _HealthAwareSupabase(_RecordingSupabase):
    """Every non-health table behaves like `_RecordingSupabase` (empty
    data); the 3 Google Health tables return rows from a per-`user_id`
    dataset — lets tests prove isolation without needing a full fake
    query-builder for tables this test class doesn't care about."""

    def __init__(self, health_data_by_user_id: dict[int, list[dict]]):
        super().__init__()
        self._health_data_by_user_id = health_data_by_user_id

    def table(self, name):
        if name in HEALTH_TABLES:
            return _HealthAwareQuery(name, self.calls, self._health_data_by_user_id)
        return super().table(name)


class TestGoogleHealthContextIntegration:
    """Twin Core Phase 1: Google Health -> Twin Context. Every health table
    read must be scoped by `user_id` (never `email`, since these tables are
    user_id-keyed) and must never leak another user's rows."""

    def test_health_tables_are_scoped_by_user_id_never_by_email(self, monkeypatch):
        fake_supabase = _HealthAwareSupabase({1: [{"user_id": 1, "data_type": "steps", "start_time": "2026-08-10T08:00:00+00:00", "value": 5000}]})
        monkeypatch.setattr(chat_module, "supabase", fake_supabase)
        monkeypatch.setattr(chat_module, "get_user_id_by_email", lambda email: 1)

        chat_module._build_context_for_user("user-a@example.com", "free")

        health_calls = [c for c in fake_supabase.calls if c[0] in HEALTH_TABLES]
        assert health_calls  # the health tables were actually queried
        assert any(field == "user_id" for _, field, _ in health_calls)
        assert not any(field == "email" for _, field, _ in health_calls)

    def test_user_a_google_health_data_never_appears_in_user_b_context(self, monkeypatch):
        health_data = {
            1: [{"user_id": 1, "data_type": "steps", "start_time": "2026-08-10T08:00:00+00:00", "value": 111111}],
            2: [{"user_id": 2, "data_type": "steps", "start_time": "2026-08-10T08:00:00+00:00", "value": 222222}],
        }

        monkeypatch.setattr(chat_module, "supabase", _HealthAwareSupabase(health_data))
        monkeypatch.setattr(chat_module, "get_user_id_by_email", lambda email: 1 if email == "user-a@example.com" else 2)

        text_a, _, _, _ = chat_module._build_context_for_user("user-a@example.com", "free")
        text_b, _, _, _ = chat_module._build_context_for_user("user-b@example.com", "free")

        assert "111111" not in text_b
        assert "222222" not in text_a

    def test_missing_user_id_yields_empty_google_health_context_without_raising(self, monkeypatch):
        monkeypatch.setattr(chat_module, "supabase", _RecordingSupabase())
        monkeypatch.setattr(chat_module, "get_user_id_by_email", lambda email: None)

        text, sources, truncated, profile = chat_module._build_context_for_user("user-a@example.com", "free")
        assert isinstance(text, str)

    def test_context_size_limit_is_still_respected_with_google_health_data(self, monkeypatch):
        long_rows = [
            {"user_id": 1, "data_type": "steps", "start_time": f"2026-08-{d:02d}T08:00:00+00:00", "value": 5000}
            for d in range(1, 8)
        ]
        monkeypatch.setattr(chat_module, "supabase", _HealthAwareSupabase({1: long_rows}))
        monkeypatch.setattr(chat_module, "get_user_id_by_email", lambda email: 1)
        monkeypatch.setattr(chat_module, "get_context_char_limit", lambda plan: 10)

        text, sources, truncated, profile = chat_module._build_context_for_user("user-a@example.com", "free")
        assert truncated is True
        assert len(text) <= 10 + 200  # quality note always fits; everything else must be dropped, never overflow wildly

    def test_family_scenario_each_members_own_user_id_stays_isolated(self, monkeypatch):
        """Family membership must never grant access to another member's
        Google Health data — proven the same way as User A/B isolation
        above, since `_build_google_health_context` has no concept of
        "family" at all and only ever resolves the SINGLE requesting
        user's own `user_id`."""
        health_data = {
            10: [{"user_id": 10, "data_type": "weight", "observed_at": "2026-08-10T08:00:00+00:00", "value": 70}],
            11: [{"user_id": 11, "data_type": "weight", "observed_at": "2026-08-10T08:00:00+00:00", "value": 99}],
        }
        monkeypatch.setattr(chat_module, "supabase", _HealthAwareSupabase(health_data))
        monkeypatch.setattr(chat_module, "get_user_id_by_email", lambda email: 10 if email == "family-owner@example.com" else 11)

        owner_text, _, _, _ = chat_module._build_context_for_user("family-owner@example.com", "free")
        member_text, _, _, _ = chat_module._build_context_for_user("family-member@example.com", "free")

        assert "70" in owner_text and "99" not in owner_text
        assert "99" in member_text and "70" not in member_text


CGM_NUTRITION_TABLES = {chat_module.CGM_TABLE, chat_module.NUTRITION_TABLE}


class _CGMNutritionAwareSupabase(_RecordingSupabase):
    """`vt_cgm_readings`/`vt_nutrition_entries` are `email`-keyed (unlike
    Google Health's `user_id`-keyed tables) — this fake returns rows from a
    per-email dataset for those 2 tables only."""

    def __init__(self, cgm_by_email: dict[str, list[dict]] | None = None, nutrition_by_email: dict[str, list[dict]] | None = None):
        super().__init__()
        self._cgm_by_email = cgm_by_email or {}
        self._nutrition_by_email = nutrition_by_email or {}

    def table(self, name):
        if name == chat_module.CGM_TABLE:
            return _EmailAwareQuery(name, self.calls, self._cgm_by_email)
        if name == chat_module.NUTRITION_TABLE:
            return _EmailAwareQuery(name, self.calls, self._nutrition_by_email)
        return super().table(name)


class _EmailAwareQuery(_RecordingQuery):
    def __init__(self, table_name, calls_log, data_by_email):
        super().__init__(table_name, calls_log)
        self._data_by_email = data_by_email
        self._filtered_email = None

    def eq(self, field, value):
        super().eq(field, value)
        if field == "email":
            self._filtered_email = value
        return self

    def execute(self):
        return SimpleNamespace(data=list(self._data_by_email.get(self._filtered_email, [])))


class TestCGMNutritionContextIntegration:
    """Twin Core Phase 2: CGM/Nutrition -> Twin Context. Every table read
    must be scoped by the requesting user's own `email` and must never leak
    another user's rows."""

    def test_cgm_table_is_scoped_by_the_requesting_email(self, monkeypatch):
        fake_supabase = _CGMNutritionAwareSupabase(
            cgm_by_email={"user-a@example.com": [{"glucose_value": 110, "reading_at": "2026-08-10T08:00:00+00:00", "source": "libreview"}]}
        )
        monkeypatch.setattr(chat_module, "supabase", fake_supabase)

        chat_module._build_context_for_user("user-a@example.com", "free")

        cgm_calls = [c for c in fake_supabase.calls if c[0] == chat_module.CGM_TABLE]
        assert ("vt_cgm_readings", "email", "user-a@example.com") in cgm_calls

    def test_user_a_cgm_data_never_appears_in_user_b_context(self, monkeypatch):
        cgm_data = {
            "user-a@example.com": [{"glucose_value": 111.0, "reading_at": "2026-08-10T08:00:00+00:00", "source": "libreview"}],
            "user-b@example.com": [{"glucose_value": 222.0, "reading_at": "2026-08-10T08:00:00+00:00", "source": "libreview"}],
        }
        monkeypatch.setattr(chat_module, "supabase", _CGMNutritionAwareSupabase(cgm_by_email=cgm_data))

        text_a, _, _, _ = chat_module._build_context_for_user("user-a@example.com", "free")
        text_b, _, _, _ = chat_module._build_context_for_user("user-b@example.com", "free")

        assert "111.0" not in text_b
        assert "222.0" not in text_a

    def test_user_a_nutrition_data_never_appears_in_user_b_context(self, monkeypatch):
        nutrition_data = {
            "user-a@example.com": [{"calories": 1111, "protein": 0, "carbs": 0, "fat": 0, "logged_at": "2026-08-10T12:00:00+00:00"}],
            "user-b@example.com": [{"calories": 2222, "protein": 0, "carbs": 0, "fat": 0, "logged_at": "2026-08-10T12:00:00+00:00"}],
        }
        monkeypatch.setattr(chat_module, "supabase", _CGMNutritionAwareSupabase(nutrition_by_email=nutrition_data))

        text_a, _, _, _ = chat_module._build_context_for_user("user-a@example.com", "free")
        text_b, _, _, _ = chat_module._build_context_for_user("user-b@example.com", "free")

        assert "1111" not in text_b
        assert "2222" not in text_a

    def test_no_cgm_or_nutrition_data_yields_no_block_without_raising(self, monkeypatch):
        monkeypatch.setattr(chat_module, "supabase", _RecordingSupabase())

        text, sources, truncated, profile = chat_module._build_context_for_user("user-a@example.com", "free")
        assert "Glukosedaten" not in text
        assert "Ernährungsdaten" not in text

    def test_family_scenario_each_members_own_email_stays_isolated(self, monkeypatch):
        """Family membership must never grant access to another member's
        CGM/Nutrition data — `_build_context_for_user` only ever resolves
        the SINGLE requesting user's own email, with no Family concept
        anywhere in this code path."""
        cgm_data = {
            "family-owner@example.com": [{"glucose_value": 105.0, "reading_at": "2026-08-10T08:00:00+00:00", "source": "libreview"}],
            "family-member@example.com": [{"glucose_value": 199.0, "reading_at": "2026-08-10T08:00:00+00:00", "source": "libreview"}],
        }
        monkeypatch.setattr(chat_module, "supabase", _CGMNutritionAwareSupabase(cgm_by_email=cgm_data))

        owner_text, _, _, _ = chat_module._build_context_for_user("family-owner@example.com", "free")
        member_text, _, _, _ = chat_module._build_context_for_user("family-member@example.com", "free")

        assert "105.0" in owner_text and "199.0" not in owner_text
        assert "199.0" in member_text and "105.0" not in member_text


class _BiomarkerAwareSupabase(_RecordingSupabase):
    """`vt_twin_calculations` is `email`-keyed (same identity scheme as
    CGM/Nutrition) — this fake returns rows from a per-email dataset for
    that table only."""

    def __init__(self, calculations_by_email: dict[str, list[dict]] | None = None):
        super().__init__()
        self._calculations_by_email = calculations_by_email or {}

    def table(self, name):
        if name == chat_module.BIOMARKER_CALC_TABLE:
            return _EmailAwareQuery(name, self.calls, self._calculations_by_email)
        return super().table(name)


class TestBiomarkerContextIntegration:
    """Twin Core Phase 4: Biomarker Twin -> Twin Context. Every read must be
    scoped by the requesting user's own `email` and must never leak
    another user's calculation."""

    def test_biomarker_table_is_scoped_by_the_requesting_email(self, monkeypatch):
        fake_supabase = _BiomarkerAwareSupabase(
            calculations_by_email={"user-a@example.com": [{"created_at": "2026-08-10T08:00:00+00:00", "biologisches_alter": 40.0, "differenz": 0.0, "scenarios": {}, "marker_breakdown": []}]}
        )
        monkeypatch.setattr(chat_module, "supabase", fake_supabase)

        chat_module._build_context_for_user("user-a@example.com", "free")

        biomarker_calls = [c for c in fake_supabase.calls if c[0] == chat_module.BIOMARKER_CALC_TABLE]
        assert ("vt_twin_calculations", "email", "user-a@example.com") in biomarker_calls

    def test_user_a_biomarker_data_never_appears_in_user_b_context(self, monkeypatch):
        calc_data = {
            "user-a@example.com": [{"created_at": "2026-08-10T08:00:00+00:00", "biologisches_alter": 31.5, "differenz": -8.5, "scenarios": {}, "marker_breakdown": []}],
            "user-b@example.com": [{"created_at": "2026-08-10T08:00:00+00:00", "biologisches_alter": 62.5, "differenz": 22.5, "scenarios": {}, "marker_breakdown": []}],
        }
        monkeypatch.setattr(chat_module, "supabase", _BiomarkerAwareSupabase(calculations_by_email=calc_data))

        text_a, _, _, _ = chat_module._build_context_for_user("user-a@example.com", "free")
        text_b, _, _, _ = chat_module._build_context_for_user("user-b@example.com", "free")

        assert "31.5" in text_a and "62.5" not in text_a
        assert "62.5" in text_b and "31.5" not in text_b

    def test_no_calculation_yields_no_block_without_raising(self, monkeypatch):
        monkeypatch.setattr(chat_module, "supabase", _RecordingSupabase())

        text, sources, truncated, profile = chat_module._build_context_for_user("user-a@example.com", "free")
        assert "Biomarker" not in text

    def test_family_scenario_each_members_own_email_stays_isolated(self, monkeypatch):
        calc_data = {
            "family-owner@example.com": [{"created_at": "2026-08-10T08:00:00+00:00", "biologisches_alter": 33.0, "differenz": -7.0, "scenarios": {}, "marker_breakdown": []}],
            "family-member@example.com": [{"created_at": "2026-08-10T08:00:00+00:00", "biologisches_alter": 55.0, "differenz": 15.0, "scenarios": {}, "marker_breakdown": []}],
        }
        monkeypatch.setattr(chat_module, "supabase", _BiomarkerAwareSupabase(calculations_by_email=calc_data))

        owner_text, _, _, _ = chat_module._build_context_for_user("family-owner@example.com", "free")
        member_text, _, _, _ = chat_module._build_context_for_user("family-member@example.com", "free")

        assert "33.0" in owner_text and "55.0" not in owner_text
        assert "55.0" in member_text and "33.0" not in member_text


class _SnapshotAwareSupabase(_RecordingSupabase):
    """`vt_twin_context_snapshots` is `email`-keyed (Twin Core Phase 7) —
    returns rows from a per-email dataset for that table only."""

    def __init__(self, snapshots_by_email: dict[str, list[dict]] | None = None):
        super().__init__()
        self._snapshots_by_email = snapshots_by_email or {}

    def table(self, name):
        if name == chat_module.SNAPSHOT_TABLE:
            return _EmailAwareQuery(name, self.calls, self._snapshots_by_email)
        return super().table(name)


class TestTwinHistoryContextIntegration:
    """Twin Core Phase 7: Twin State Snapshots -> Twin Context. Never sends
    raw snapshot JSON to the LLM; scoped strictly by the requesting user's
    own email."""

    def test_snapshot_table_is_scoped_by_the_requesting_email(self, monkeypatch):
        fake_supabase = _SnapshotAwareSupabase(
            snapshots_by_email={
                "user-a@example.com": [
                    {"created_at": "2026-08-01T08:00:00+00:00", "snapshot": {"domains": {"behavioral_wellness": {"status": "current", "values": {"trends": {"sleep_hours": {"average": 6.0}}}}}}},
                    {"created_at": "2026-08-10T08:00:00+00:00", "snapshot": {"domains": {"behavioral_wellness": {"status": "current", "values": {"trends": {"sleep_hours": {"average": 7.5}}}}}}},
                ]
            }
        )
        monkeypatch.setattr(chat_module, "supabase", fake_supabase)

        chat_module._build_context_for_user("user-a@example.com", "free")

        snapshot_calls = [c for c in fake_supabase.calls if c[0] == chat_module.SNAPSHOT_TABLE]
        assert ("vt_twin_context_snapshots", "email", "user-a@example.com") in snapshot_calls

    def test_raw_snapshot_json_never_appears_in_context_text(self, monkeypatch):
        fake_supabase = _SnapshotAwareSupabase(
            snapshots_by_email={
                "user-a@example.com": [
                    {"created_at": "2026-08-01T08:00:00+00:00", "snapshot": {"domains": {"behavioral_wellness": {"status": "current", "values": {"trends": {"sleep_hours": {"average": 6.0}}}}}}},
                    {"created_at": "2026-08-10T08:00:00+00:00", "snapshot": {"domains": {"behavioral_wellness": {"status": "current", "values": {"trends": {"sleep_hours": {"average": 7.5}}}}}}},
                ]
            }
        )
        monkeypatch.setattr(chat_module, "supabase", fake_supabase)

        text, sources, _, _ = chat_module._build_context_for_user("user-a@example.com", "free")
        assert "domains" not in text
        assert "gestiegen" in text
        assert any(s["type"] == "twin_history" for s in sources)

    def test_fewer_than_two_snapshots_yields_no_block(self, monkeypatch):
        fake_supabase = _SnapshotAwareSupabase(
            snapshots_by_email={"user-a@example.com": [{"created_at": "2026-08-10T08:00:00+00:00", "snapshot": {}}]}
        )
        monkeypatch.setattr(chat_module, "supabase", fake_supabase)

        text, _, _, _ = chat_module._build_context_for_user("user-a@example.com", "free")
        assert "Entwicklung seit der letzten Aufzeichnung" not in text

    def test_user_a_snapshot_history_never_appears_for_user_b(self, monkeypatch):
        data = {
            "user-a@example.com": [
                {"created_at": "2026-08-01T08:00:00+00:00", "snapshot": {"domains": {"behavioral_wellness": {"status": "current", "values": {"trends": {"sleep_hours": {"average": 6.0}}}}}}},
                {"created_at": "2026-08-10T08:00:00+00:00", "snapshot": {"domains": {"behavioral_wellness": {"status": "current", "values": {"trends": {"sleep_hours": {"average": 9.0}}}}}}},
            ],
        }
        monkeypatch.setattr(chat_module, "supabase", _SnapshotAwareSupabase(snapshots_by_email=data))

        text_a, _, _, _ = chat_module._build_context_for_user("user-a@example.com", "free")
        text_b, _, _, _ = chat_module._build_context_for_user("user-b@example.com", "free")

        assert "gestiegen" in text_a
        assert "Entwicklung seit der letzten Aufzeichnung" not in text_b

