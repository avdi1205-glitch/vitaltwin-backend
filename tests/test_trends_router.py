"""Unit tests for `app.routers.profile::get_trends` — specifically the
"Erweiterter Verlauf" plan-gating added for Premium/Pro/Family (see
`lib/plans.ts` on the frontend). Mocks Supabase and auth/premium lookup —
no real network/database access.

Free must keep exactly the previous behavior (30-row limit, 7d/30d windows
only). Premium/Pro/Family (all share the same `vt_users.premium` boolean
today, see `routers/users.py::is_premium_by_email`) get a 90-row limit and
an additional 90d window. The gating is server-side (`is_premium_by_email`
checked against the authenticated user's own account), so a Free user
cannot obtain the extended window by calling this endpoint directly."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.routers import profile as profile_module


class _RecordingQuery:
    def __init__(self, calls_log: list[dict[str, object]], data=None):
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


class TestTrendsPlanGating:
    @pytest.mark.anyio
    async def test_free_user_gets_thirty_row_limit_and_two_windows(self, monkeypatch):
        fake = _RecordingSupabase(data=[])
        monkeypatch.setattr(profile_module, "supabase", fake)
        monkeypatch.setattr(profile_module, "_require_email", lambda auth: "free-user@example.com")
        monkeypatch.setattr(profile_module, "is_premium_by_email", lambda email: False)

        result = await profile_module.get_trends(authorization="Bearer x")

        assert fake.calls[0]["limit"] == 30
        assert result["extended_history"] is False
        assert set(result["trends"]["sleep_hours"].keys()) == {"7d", "30d"}

    @pytest.mark.anyio
    async def test_premium_user_gets_ninety_row_limit_and_three_windows(self, monkeypatch):
        fake = _RecordingSupabase(data=[])
        monkeypatch.setattr(profile_module, "supabase", fake)
        monkeypatch.setattr(profile_module, "_require_email", lambda auth: "premium-user@example.com")
        monkeypatch.setattr(profile_module, "is_premium_by_email", lambda email: True)

        result = await profile_module.get_trends(authorization="Bearer x")

        assert fake.calls[0]["limit"] == 90
        assert result["extended_history"] is True
        assert set(result["trends"]["sleep_hours"].keys()) == {"7d", "30d", "90d"}

    @pytest.mark.anyio
    async def test_gating_is_based_on_the_authenticated_users_own_account(self, monkeypatch):
        """A Free user cannot get the extended window by calling this
        endpoint directly (no frontend-only lock): `is_premium_by_email`
        is always looked up server-side from the caller's own email."""
        fake = _RecordingSupabase(data=[])
        monkeypatch.setattr(profile_module, "supabase", fake)
        monkeypatch.setattr(profile_module, "_require_email", lambda auth: "free-user@example.com")

        seen_emails: list[str] = []

        def fake_is_premium(email: str) -> bool:
            seen_emails.append(email)
            return False

        monkeypatch.setattr(profile_module, "is_premium_by_email", fake_is_premium)

        result = await profile_module.get_trends(authorization="Bearer x")

        assert seen_emails == ["free-user@example.com"]
        assert result["extended_history"] is False
