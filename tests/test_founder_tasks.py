"""Tests for the AI Founder Task Manager (VitalTwin Release F3 — Founder
Operating System, Module 3): `core/founder_task_detector.py` (rule-based
detection engine, idempotent, no LLM call) and `routers/founder_tasks.py`
(the API — permission checks, status transitions, suggestion execution)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core import founder_task_detector as detector_module
from app.routers import founder_tasks as tasks_module


@pytest.fixture
def anyio_backend():
    return "asyncio"


class _FakeTaskQuery:
    def __init__(self, table_rows: list[dict]):
        self._table_rows = table_rows
        self._predicates = []
        self._pending_insert = None
        self._pending_update = None
        self._limit_n = None

    def select(self, *a, **k):
        return self

    def eq(self, field, value):
        self._predicates.append(lambda row, f=field, v=value: row.get(f) == v)
        return self

    def gte(self, field, value):
        self._predicates.append(lambda row, f=field, v=value: str(row.get(f, "")) >= str(value))
        return self

    def order(self, *a, **k):
        return self

    def limit(self, n):
        self._limit_n = n
        return self

    def insert(self, payload):
        self._pending_insert = payload
        return self

    def update(self, payload):
        self._pending_update = payload
        return self

    def _matching(self):
        rows = [r for r in self._table_rows if all(p(r) for p in self._predicates)]
        if self._limit_n is not None:
            rows = rows[: self._limit_n]
        return rows

    def execute(self):
        if self._pending_insert is not None:
            new_row = dict(self._pending_insert)
            new_row.setdefault("id", f"id-{len(self._table_rows) + 1}")
            self._table_rows.append(new_row)
            return SimpleNamespace(data=[new_row], count=1)
        if self._pending_update is not None:
            matched = [r for r in self._table_rows if all(p(r) for p in self._predicates)]
            for row in matched:
                row.update(self._pending_update)
            return SimpleNamespace(data=matched, count=len(matched))
        rows = self._matching()
        return SimpleNamespace(data=rows, count=len(rows))


class _FakeTaskSupabase:
    def __init__(self, tables: dict[str, list[dict]] | None = None):
        self.tables = tables or {}

    def table(self, name):
        rows = self.tables.setdefault(name, [])
        return _FakeTaskQuery(rows)


@pytest.fixture
def task_supabase(monkeypatch):
    fake = _FakeTaskSupabase()
    monkeypatch.setattr(detector_module, "supabase", fake)
    monkeypatch.setattr(tasks_module, "supabase", fake)
    return fake


@pytest.fixture
def tasks_permission_spy(monkeypatch):
    calls: list[tuple] = []

    def _fake(authorization, permission):
        calls.append((authorization, permission))
        return SimpleNamespace(email="founder@example.com", role="super_admin")

    monkeypatch.setattr(tasks_module, "require_admin_permission", _fake)
    return calls


class TestDetectorIdempotency:
    def test_creates_task_when_condition_true_and_none_exists(self, task_supabase):
        task_supabase.tables["vt_affiliate_products"] = [
            {"id": "p1", "title": "T1", "link_status": "broken", "status": "active"},
        ]
        detector_module._detect_affiliate_broken_links()
        tasks = task_supabase.tables["vt_founder_tasks"]
        assert len(tasks) == 1
        assert tasks[0]["dedupe_key"] == "affiliate_broken_links"
        assert tasks[0]["status"] == "neu"
        assert tasks[0]["auto_detected"] is True

    def test_does_not_duplicate_on_second_scan(self, task_supabase):
        task_supabase.tables["vt_affiliate_products"] = [
            {"id": "p1", "title": "T1", "link_status": "broken", "status": "active"},
        ]
        detector_module._detect_affiliate_broken_links()
        detector_module._detect_affiliate_broken_links()
        tasks = task_supabase.tables["vt_founder_tasks"]
        assert len(tasks) == 1

    def test_auto_resolves_when_condition_clears(self, task_supabase):
        task_supabase.tables["vt_affiliate_products"] = [
            {"id": "p1", "title": "T1", "link_status": "broken", "status": "active"},
        ]
        detector_module._detect_affiliate_broken_links()
        # The link is fixed now:
        task_supabase.tables["vt_affiliate_products"][0]["link_status"] = "ok"
        detector_module._detect_affiliate_broken_links()
        tasks = task_supabase.tables["vt_founder_tasks"]
        assert tasks[0]["status"] == "erledigt"
        assert tasks[0]["auto_resolved"] is True

    def test_does_not_reopen_a_task_the_founder_already_closed(self, task_supabase):
        task_supabase.tables["vt_affiliate_products"] = [
            {"id": "p1", "title": "T1", "link_status": "broken", "status": "active"},
        ]
        detector_module._detect_affiliate_broken_links()
        # Founder manually archives/ignores it while the link is still broken:
        task_supabase.tables["vt_founder_tasks"][0]["status"] = "archiviert"
        task_supabase.tables["vt_founder_tasks"][0]["ignored"] = True
        detector_module._detect_affiliate_broken_links()
        tasks = task_supabase.tables["vt_founder_tasks"]
        assert len(tasks) == 1
        assert tasks[0]["status"] == "archiviert"

    def test_priority_escalates_with_more_broken_links(self, task_supabase):
        task_supabase.tables["vt_affiliate_products"] = [
            {"id": f"p{i}", "title": f"T{i}", "link_status": "broken", "status": "active"} for i in range(3)
        ]
        detector_module._detect_affiliate_broken_links()
        assert task_supabase.tables["vt_founder_tasks"][0]["priority"] == "kritisch"

    def test_stripe_not_configured_creates_critical_task(self, task_supabase, monkeypatch):
        monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
        detector_module._detect_stripe_not_configured()
        tasks = task_supabase.tables["vt_founder_tasks"]
        assert tasks[0]["dedupe_key"] == "premium_stripe_not_configured"
        assert tasks[0]["priority"] == "kritisch"
        assert tasks[0]["category"] == "premium"

    def test_stripe_configured_creates_no_task(self, task_supabase, monkeypatch):
        monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_dummy")
        detector_module._detect_stripe_not_configured()
        assert task_supabase.tables.get("vt_founder_tasks", []) == []

    def test_failed_login_spike_requires_threshold(self, task_supabase):
        task_supabase.tables["vt_login_events"] = [
            {"email": "a@example.com", "success": False, "created_at": "2099-01-01T00:00:00+00:00"} for _ in range(3)
        ]
        detector_module._detect_failed_login_spike()
        assert task_supabase.tables.get("vt_founder_tasks", []) == []

    def test_failed_login_spike_above_threshold_creates_task(self, task_supabase):
        task_supabase.tables["vt_login_events"] = [
            {"email": "a@example.com", "success": False, "created_at": "2099-01-01T00:00:00+00:00"} for _ in range(6)
        ]
        detector_module._detect_failed_login_spike()
        tasks = task_supabase.tables["vt_founder_tasks"]
        assert tasks[0]["source"] == "sicherheit"
        assert tasks[0]["priority"] == "hoch"

    def test_run_detection_runs_all_rules_without_crashing(self, task_supabase, monkeypatch):
        monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        detector_module.run_detection()
        # At minimum stripe + openai not-configured tasks should exist.
        dedupe_keys = {t["dedupe_key"] for t in task_supabase.tables["vt_founder_tasks"]}
        assert "premium_stripe_not_configured" in dedupe_keys
        assert "ki_openai_not_configured" in dedupe_keys


class TestFounderTasksRouter:
    @pytest.mark.anyio
    async def test_list_tasks_requires_view_permission(self, task_supabase, tasks_permission_spy):
        await tasks_module.list_founder_tasks(authorization="Bearer x")
        assert tasks_permission_spy[-1] == ("Bearer x", "view_founder_os")

    @pytest.mark.anyio
    async def test_list_tasks_runs_detection_and_returns_summary(self, task_supabase, tasks_permission_spy, monkeypatch):
        monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
        result = await tasks_module.list_founder_tasks(authorization="Bearer x")
        assert result["summary"]["open_tasks"] >= 1
        assert result["summary"]["critical_tasks"] >= 1

    @pytest.mark.anyio
    async def test_update_status_requires_manage_permission(self, task_supabase, tasks_permission_spy):
        task_supabase.tables["vt_founder_tasks"] = [{"id": "t1", "status": "neu"}]
        data = tasks_module.StatusInput(status="in_bearbeitung")
        await tasks_module.update_task_status("t1", data, authorization="Bearer x")
        assert tasks_permission_spy[-1] == ("Bearer x", "manage_founder_os")
        assert task_supabase.tables["vt_founder_tasks"][0]["status"] == "in_bearbeitung"

    @pytest.mark.anyio
    async def test_invalid_status_is_rejected(self):
        with pytest.raises(ValueError):
            tasks_module.StatusInput(status="not_a_real_status")

    @pytest.mark.anyio
    async def test_remind_sets_status_warten_and_remind_at(self, task_supabase, tasks_permission_spy):
        task_supabase.tables["vt_founder_tasks"] = [{"id": "t1", "status": "neu"}]
        result = await tasks_module.remind_later("t1", authorization="Bearer x")
        assert task_supabase.tables["vt_founder_tasks"][0]["status"] == "warten"
        assert "remind_at" in result

    @pytest.mark.anyio
    async def test_ignore_archives_and_marks_ignored(self, task_supabase, tasks_permission_spy):
        task_supabase.tables["vt_founder_tasks"] = [{"id": "t1", "status": "neu"}]
        await tasks_module.ignore_task("t1", authorization="Bearer x")
        assert task_supabase.tables["vt_founder_tasks"][0]["status"] == "archiviert"
        assert task_supabase.tables["vt_founder_tasks"][0]["ignored"] is True

    @pytest.mark.anyio
    async def test_apply_suggestion_rejects_tasks_without_real_automation(self, task_supabase, tasks_permission_spy):
        task_supabase.tables["vt_founder_tasks"] = [
            {"id": "t1", "dedupe_key": "premium_stripe_not_configured", "suggested_action_available": False}
        ]
        with pytest.raises(HTTPException) as exc_info:
            await tasks_module.apply_suggestion("t1", authorization="Bearer x")
        assert exc_info.value.status_code == 400

    @pytest.mark.anyio
    async def test_apply_suggestion_rechecks_broken_links(self, task_supabase, tasks_permission_spy, monkeypatch):
        task_supabase.tables["vt_founder_tasks"] = [
            {"id": "t1", "dedupe_key": "affiliate_broken_links", "suggested_action_available": True}
        ]
        task_supabase.tables["vt_affiliate_products"] = [
            {"id": "p1", "affiliate_url": "https://example.com/ok", "link_status": "broken"},
            {"id": "p2", "affiliate_url": "https://example.com/still-broken", "link_status": "broken"},
        ]
        monkeypatch.setattr(
            tasks_module,
            "check_link",
            lambda url: {"link_status": "ok", "http_status": 200, "redirected": False}
            if url == "https://example.com/ok"
            else {"link_status": "broken", "http_status": 404, "redirected": False},
        )
        result = await tasks_module.apply_suggestion("t1", authorization="Bearer x")
        assert result["fixed"] == 1
        assert result["still_broken"] == 1
        products = task_supabase.tables["vt_affiliate_products"]
        assert products[0]["link_status"] == "ok"
        assert products[1]["link_status"] == "broken"
