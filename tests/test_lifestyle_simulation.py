"""Unit tests for the Pro feature "Lifestyle-Simulationen" (Wellness-
Szenarien) — `services/lifestyle_simulation.py` and
`app.routers.profile::simulate_lifestyle_change`. Mocks Supabase and the
entitlement lookup — no real network/database access.

Constitution rule 10: simulations only, never a medical prediction — every
result carries the fixed `SIMULATION_DISCLAIMER`. Free/Premium are blocked
server-side (403); Pro/Family get a real arithmetic recompute of their own
7-day average."""

from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.routers import profile as profile_module
from app.routers.profile import SimulationRequest
from app.services.lifestyle_simulation import (
    FIELD_BOUNDS,
    SIMULATION_DISCLAIMER,
    simulate_metric_change,
)


def _entries_with_constant_value(field: str, value: float, days: int = 7) -> list[dict[str, object]]:
    today = date.today()
    return [{"entry_date": (today - timedelta(days=i)).isoformat(), field: value} for i in range(days)]


class TestSimulateMetricChange:
    def test_adds_delta_to_the_real_current_average(self):
        entries = _entries_with_constant_value("movement_minutes", 30)
        result = simulate_metric_change(entries, field="movement_minutes", delta=15, today=date.today())
        assert result.current_average == 30
        assert result.simulated_average == 45
        assert result.disclaimer == SIMULATION_DISCLAIMER

    def test_clamps_to_the_fields_real_valid_range(self):
        entries = _entries_with_constant_value("stress", 9)
        result = simulate_metric_change(entries, field="stress", delta=5, today=date.today())
        # stress is bounded 1-10 (core/validation.py SCALE_MIN/SCALE_MAX)
        assert result.simulated_average == FIELD_BOUNDS["stress"][1] == 10

    def test_clamps_at_the_lower_bound_too(self):
        entries = _entries_with_constant_value("sleep_hours", 1)
        result = simulate_metric_change(entries, field="sleep_hours", delta=-5, today=date.today())
        assert result.simulated_average == FIELD_BOUNDS["sleep_hours"][0] == 0

    def test_returns_none_without_fabricating_a_number_when_no_data_exists(self):
        result = simulate_metric_change([], field="sleep_hours", delta=1, today=date.today())
        assert result.current_average is None
        assert result.simulated_average is None
        assert result.data_quality == "missing"


class _RecordingQuery:
    def __init__(self, calls_log, data=None):
        self._calls_log = calls_log
        self._data = data if data is not None else []
        self._state: dict[str, object] = {}

    def select(self, *args, **kwargs):
        return self

    def eq(self, field, value):
        self._state[field] = value
        return self

    def order(self, *args, **kwargs):
        return self

    def limit(self, value):
        self._state["limit"] = value
        return self

    def execute(self):
        self._calls_log.append(dict(self._state))
        return SimpleNamespace(data=self._data)


class _RecordingSupabase:
    def __init__(self, data=None):
        self.calls: list[dict[str, object]] = []
        self._data = data

    def table(self, name):
        return _RecordingQuery(self.calls, self._data)


class TestSimulateLifestyleChangeEndpoint:
    @pytest.mark.anyio
    async def test_free_user_is_blocked(self, monkeypatch):
        fake = _RecordingSupabase(data=[])
        monkeypatch.setattr(profile_module, "supabase", fake)
        monkeypatch.setattr(profile_module, "_require_email", lambda auth: "free@example.com")
        monkeypatch.setattr(profile_module, "has_feature", lambda email, feature: False)

        with pytest.raises(HTTPException) as exc_info:
            await profile_module.simulate_lifestyle_change(
                SimulationRequest(field="sleep_hours", delta=1), authorization="Bearer x"
            )
        assert exc_info.value.status_code == 403

    @pytest.mark.anyio
    async def test_pro_user_gets_a_real_recompute(self, monkeypatch):
        entries = _entries_with_constant_value("movement_minutes", 20)
        fake = _RecordingSupabase(data=entries)
        monkeypatch.setattr(profile_module, "supabase", fake)
        monkeypatch.setattr(profile_module, "_require_email", lambda auth: "pro@example.com")
        monkeypatch.setattr(profile_module, "has_feature", lambda email, feature: True)

        result = await profile_module.simulate_lifestyle_change(
            SimulationRequest(field="movement_minutes", delta=10), authorization="Bearer x"
        )
        assert result["current_average"] == 20
        assert result["simulated_average"] == 30
        assert result["disclaimer"] == SIMULATION_DISCLAIMER

    @pytest.mark.anyio
    async def test_query_is_scoped_to_the_requesting_users_own_email(self, monkeypatch):
        fake = _RecordingSupabase(data=[])
        monkeypatch.setattr(profile_module, "supabase", fake)
        monkeypatch.setattr(profile_module, "_require_email", lambda auth: "user-a@example.com")
        monkeypatch.setattr(profile_module, "has_feature", lambda email, feature: True)

        await profile_module.simulate_lifestyle_change(
            SimulationRequest(field="stress", delta=1), authorization="Bearer x"
        )
        assert fake.calls[0]["email"] == "user-a@example.com"

    def test_rejects_a_field_without_real_stored_data(self):
        with pytest.raises(ValueError):
            SimulationRequest(field="heart_rate", delta=1)  # type: ignore[arg-type]
