"""Unit tests for the Etappe 6 Pydantic validation models and ownership
helper in `app.routers.daily_planning`. Mocks the Supabase client for the
ownership (Nutzertrennung) test — no real network/database access."""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.routers import daily_planning as daily_planning_router
from app.routers.daily_planning import ActionAdjustmentInput, ActionDecisionInput, ReflectionInput


class _FakeResponse:
    def __init__(self, data):
        self.data = data


class _FakeQuery:
    def __init__(self, data):
        self._data = data

    def select(self, *args, **kwargs):
        return self

    def eq(self, *args, **kwargs):
        return self

    def in_(self, *args, **kwargs):
        return self

    def is_(self, *args, **kwargs):
        return self

    def order(self, *args, **kwargs):
        return self

    def limit(self, *args, **kwargs):
        return self

    def execute(self):
        return _FakeResponse(self._data)


class _FakeSupabase:
    def __init__(self, data):
        self._data = data

    def table(self, name):
        return _FakeQuery(self._data)


class TestActionAdjustmentInput:
    def test_strips_whitespace(self):
        model = ActionAdjustmentInput(description="  Neue Beschreibung  ")
        assert model.description == "Neue Beschreibung"

    def test_rejects_empty_description(self):
        with pytest.raises(ValidationError):
            ActionAdjustmentInput(description="   ")

    def test_rejects_too_long_description(self):
        with pytest.raises(ValidationError):
            ActionAdjustmentInput(description="x" * 281)


class TestActionDecisionInput:
    @pytest.mark.parametrize("decision", ["accepted", "rejected"])
    def test_accepts_valid_decisions(self, decision):
        assert ActionDecisionInput(decision=decision).decision == decision

    def test_rejects_invalid_decision(self):
        with pytest.raises(ValidationError):
            ActionDecisionInput(decision="modified")


class TestReflectionInput:
    def test_accepts_all_fields(self):
        model = ReflectionInput(
            completed_summary="Alles erledigt",
            helpful_note="Kurzer Spaziergang",
            difficult_note="Früh aufstehen",
            mood=7,
            energy=6,
            tomorrow_change="Früher schlafen",
        )
        assert model.mood == 7

    def test_rejects_out_of_range_mood(self):
        with pytest.raises(ValidationError):
            ReflectionInput(mood=11)

    def test_rejects_too_long_text(self):
        with pytest.raises(ValidationError):
            ReflectionInput(tomorrow_change="x" * 501)

    def test_all_fields_optional(self):
        model = ReflectionInput()
        assert model.mood is None
        assert model.completed_summary is None


class TestBuildMemoryCandidateNotes:
    def test_both_notes_produce_two_entries(self):
        notes = daily_planning_router._build_memory_candidate_notes("Spaziergang", "Früh aufstehen")
        assert len(notes) == 2

    def test_no_notes_produce_empty_list(self):
        assert daily_planning_router._build_memory_candidate_notes(None, None) == []


class TestRequireOwnAction:
    def test_missing_action_raises_404(self, monkeypatch):
        monkeypatch.setattr(daily_planning_router, "supabase", _FakeSupabase([]))
        with pytest.raises(HTTPException) as exc_info:
            daily_planning_router._require_own_action("user-a@example.com", "does-not-exist")
        assert exc_info.value.status_code == 404

    def test_own_action_is_returned(self, monkeypatch):
        row = {"id": "a1", "email": "user-a@example.com", "status": "proposed"}
        monkeypatch.setattr(daily_planning_router, "supabase", _FakeSupabase([row]))
        result = daily_planning_router._require_own_action("user-a@example.com", "a1")
        assert result == row

    def test_foreign_action_is_not_distinguishable_from_missing(self, monkeypatch):
        monkeypatch.setattr(daily_planning_router, "supabase", _FakeSupabase([]))
        with pytest.raises(HTTPException) as exc_info:
            daily_planning_router._require_own_action("user-b@example.com", "someone-elses-action")
        assert exc_info.value.status_code == 404
