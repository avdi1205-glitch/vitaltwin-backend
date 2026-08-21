"""Targeted tests confirming the "first 20 active beta testers" discount
program hook (`core.beta_discount_program.maybe_claim_discount_slot`) fires
correctly from the check-in save path (`routers.profile.upsert_daily_entry`)
and the twin-calculation storage path (`routers.twin._store_calculation`).
The Health Connect sync hook is covered separately in
`test_health_connect.py::TestBetaDiscountProgramHook`. Mocks Supabase and
auth — no real network/database access."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.routers import profile as profile_module
from app.routers import twin as twin_module


class _FakeExecuteResult:
    def __init__(self, data=None):
        self.data = data or []


class _FakeInsertUpdateTable:
    def select(self, *a, **k):
        return self

    def eq(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def insert(self, payload):
        return self

    def update(self, payload):
        return self

    def execute(self):
        return _FakeExecuteResult(data=[])


class _FakeSupabase:
    def table(self, name):
        return _FakeInsertUpdateTable()


class TestCheckinTriggersDiscountHook:
    def test_successful_checkin_save_triggers_the_hook(self, monkeypatch):
        monkeypatch.setattr(profile_module, "supabase", _FakeSupabase())
        monkeypatch.setattr(profile_module, "record_audit_event", lambda **kwargs: None)
        monkeypatch.setattr(profile_module, "_require_email", lambda authorization: "checker@example.com")

        calls: list[str] = []
        monkeypatch.setattr(profile_module, "maybe_claim_discount_slot", lambda email: calls.append(email))

        data = profile_module.DailyWellnessEntryInput(sleep_hours=7.5)
        result = asyncio.run(profile_module.upsert_daily_entry(data, authorization="Bearer faketoken"))

        assert result["message"] == "Gespeichert."
        assert calls == ["checker@example.com"]

    def test_hook_failure_never_breaks_the_checkin_response(self, monkeypatch):
        monkeypatch.setattr(profile_module, "supabase", _FakeSupabase())
        monkeypatch.setattr(profile_module, "record_audit_event", lambda **kwargs: None)
        monkeypatch.setattr(profile_module, "_require_email", lambda authorization: "checker@example.com")

        def _boom(email):
            raise RuntimeError("discount program unavailable")

        monkeypatch.setattr(profile_module, "maybe_claim_discount_slot", _boom)

        data = profile_module.DailyWellnessEntryInput(sleep_hours=7.5)
        result = asyncio.run(profile_module.upsert_daily_entry(data, authorization="Bearer faketoken"))
        assert result["message"] == "Gespeichert."


class TestTwinCalculationTriggersDiscountHook:
    def test_successful_calculation_storage_triggers_the_hook(self, monkeypatch):
        monkeypatch.setattr(twin_module, "supabase", _FakeSupabase())
        calls: list[str] = []
        monkeypatch.setattr(twin_module, "maybe_claim_discount_slot", lambda email: calls.append(email))

        data = twin_module.TwinInput(age=30, gender="male")
        result = {"biologisches_alter": 30.0, "differenz": 0.0, "scenarios": {}}
        twin_module._store_calculation("twin-user@example.com", data, result, [])

        assert calls == ["twin-user@example.com"]

    def test_no_hook_call_when_email_is_missing(self, monkeypatch):
        monkeypatch.setattr(twin_module, "supabase", _FakeSupabase())
        calls: list[str] = []
        monkeypatch.setattr(twin_module, "maybe_claim_discount_slot", lambda email: calls.append(email))

        data = twin_module.TwinInput(age=30, gender="male")
        result = {"biologisches_alter": 30.0, "differenz": 0.0, "scenarios": {}}
        twin_module._store_calculation(None, data, result, [])

        assert calls == []

    def test_no_hook_call_when_storage_itself_fails(self, monkeypatch):
        class _BoomTable:
            def insert(self, payload):
                raise RuntimeError("db unavailable")

        class _BoomSupabase:
            def table(self, name):
                return _BoomTable()

        monkeypatch.setattr(twin_module, "supabase", _BoomSupabase())
        calls: list[str] = []
        monkeypatch.setattr(twin_module, "maybe_claim_discount_slot", lambda email: calls.append(email))

        data = twin_module.TwinInput(age=30, gender="male")
        result = {"biologisches_alter": 30.0, "differenz": 0.0, "scenarios": {}}
        twin_module._store_calculation("twin-user@example.com", data, result, [])

        assert calls == []
