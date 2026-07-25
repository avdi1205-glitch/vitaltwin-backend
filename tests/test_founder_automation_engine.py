"""Tests for the Automation Engine (VitalTwin Enterprise, Founder
Operating System, Submodule G): Safe Action Registry
(`core/automation_registry.py`), condition evaluator
(`core/automation_conditions.py`), the rule engine
(`core/automation_engine.py`), opportunity detection
(`core/automation_opportunity_detector.py`), automation score
(`core/automation_score.py`), and the API router
(`routers/founder_automation.py`) — permissions, rule lifecycle, dry run,
idempotency, retry/dead-letter, rollback, integrations."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core import admin_rbac
from app.core import automation_conditions as conditions
from app.core import automation_engine as engine
from app.core import automation_opportunity_detector as opportunity_detector
from app.core import automation_registry as registry
from app.core import automation_score as score_module
from app.routers import founder_automation as automation_router
from app.routers import founder_approval


@pytest.fixture
def anyio_backend():
    return "asyncio"


class _FakeQuery:
    def __init__(self, table_rows: list[dict], *, count_mode: bool = False):
        self._table_rows = table_rows
        self._predicates = []
        self._pending_insert = None
        self._pending_update = None
        self._limit_n = None
        self._count_mode = count_mode

    def select(self, *a, count=None, **k):
        self._count_mode = count is not None
        return self

    def eq(self, field, value):
        self._predicates.append(lambda row, f=field, v=value: row.get(f) == v)
        return self

    def in_(self, field, values):
        self._predicates.append(lambda row, f=field, v=set(values): row.get(f) in v)
        return self

    def gte(self, field, value):
        self._predicates.append(lambda row, f=field, v=value: str(row.get(f, "")) >= str(v))
        return self

    def lt(self, field, value):
        self._predicates.append(lambda row, f=field, v=value: str(row.get(f, "")) < str(v))
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


class _FakeSupabase:
    def __init__(self, tables: dict[str, list[dict]] | None = None):
        self.tables = tables or {}

    def table(self, name):
        rows = self.tables.setdefault(name, [])
        return _FakeQuery(rows)


@pytest.fixture
def fake_supabase(monkeypatch):
    fake = _FakeSupabase()
    monkeypatch.setattr(engine, "supabase", fake)
    monkeypatch.setattr(opportunity_detector, "supabase", fake)
    monkeypatch.setattr(score_module, "supabase", fake)
    monkeypatch.setattr(automation_router, "supabase", fake)
    monkeypatch.setattr(founder_approval, "supabase", fake)
    return fake


@pytest.fixture
def permission_spy(monkeypatch):
    calls: list[tuple] = []

    def _fake(authorization, permission):
        calls.append((authorization, permission))
        return SimpleNamespace(email="founder@example.com", role="super_admin")

    monkeypatch.setattr(automation_router, "require_admin_permission", _fake)
    return calls


LOW_RISK_RULE = {
    "name": "Defekte Links erneut prüfen", "description": "", "category": "affiliate",
    "trigger_type": "schedule", "trigger_config": {"interval_hours": 24}, "conditions": [],
    "actions": [{"action_type": "link_pruefen", "params": {"product_id": "p1"}}],
    "risk_level": "low", "approval_policy": "no_approval",
    "retry_policy": {"type": "fixed", "max_attempts": 2, "cooldown_seconds": 0},
    "timeout_seconds": 30, "environment": "production", "rollout_stage": "nur_founder",
}


class TestSafeActionRegistry:
    def test_no_critical_action_exists(self):
        assert all(a.risk_level != "critical" for a in registry.ACTION_REGISTRY.values())

    def test_critical_risk_level_rejected(self):
        with pytest.raises(ValueError):
            registry.validate_risk_level("critical")

    def test_unknown_action_blocked(self):
        allowed, reason = registry.is_action_allowed("preis_aendern", environment="production")
        assert not allowed
        assert "Unbekannte Aktion" in reason

    def test_known_low_risk_action_allowed(self):
        allowed, _ = registry.is_action_allowed("link_pruefen", environment="production")
        assert allowed

    def test_validate_actions_rejects_unknown_action(self):
        with pytest.raises(ValueError):
            registry.validate_actions([{"action_type": "preis_aendern"}], environment="production")

    def test_validate_actions_requires_at_least_one(self):
        with pytest.raises(ValueError):
            registry.validate_actions([], environment="production")


class TestConditionEvaluator:
    def test_equals(self):
        assert conditions.evaluate_condition({"field": "status", "operator": "equals", "value": "aktiv"}, {"status": "aktiv"})

    def test_not_equals(self):
        assert conditions.evaluate_condition({"field": "status", "operator": "not_equals", "value": "aktiv"}, {"status": "pausiert"})

    def test_greater_than(self):
        assert conditions.evaluate_condition({"field": "broken_links_count", "operator": "greater_than", "value": 0}, {"broken_links_count": 3})

    def test_less_than_false_when_equal(self):
        assert not conditions.evaluate_condition({"field": "n", "operator": "less_than", "value": 5}, {"n": 5})

    def test_missing_field(self):
        assert conditions.evaluate_condition({"field": "x", "operator": "missing"}, {})

    def test_contains(self):
        assert conditions.evaluate_condition({"field": "tags", "operator": "contains", "value": "b"}, {"tags": ["a", "b"]})

    def test_age_in_days_stale(self):
        old_date = "2000-01-01T00:00:00+00:00"
        assert conditions.evaluate_condition({"field": "created_at", "operator": "age_in_days", "value": 1}, {"created_at": old_date})

    def test_and_group(self):
        cond = {"all": [{"field": "a", "operator": "equals", "value": 1}, {"field": "b", "operator": "equals", "value": 2}]}
        assert conditions.evaluate_condition(cond, {"a": 1, "b": 2})
        assert not conditions.evaluate_condition(cond, {"a": 1, "b": 3})

    def test_or_group(self):
        cond = {"any": [{"field": "a", "operator": "equals", "value": 1}, {"field": "b", "operator": "equals", "value": 2}]}
        assert conditions.evaluate_condition(cond, {"a": 9, "b": 2})

    def test_empty_conditions_means_always_true(self):
        assert conditions.evaluate_conditions([], {})
        assert conditions.evaluate_conditions(None, {})

    def test_malformed_condition_never_raises(self):
        assert conditions.evaluate_condition({"field": None, "operator": None}, {}) is False
        assert conditions.evaluate_condition("not-a-dict", {}) is False  # type: ignore[arg-type]


class TestRuleCreationAndVersioning:
    def test_create_rule_defaults_disabled_and_draft(self, fake_supabase):
        rule = engine.create_rule(LOW_RISK_RULE, created_by="founder@example.com")
        assert rule["enabled"] is False
        assert rule["status"] == "entwurf"
        assert rule["version"] == 1

    def test_create_rule_writes_version_snapshot(self, fake_supabase):
        engine.create_rule(LOW_RISK_RULE, created_by="founder@example.com")
        versions = fake_supabase.tables["vt_automation_rule_versions"]
        assert len(versions) == 1
        assert versions[0]["version"] == 1

    def test_create_rule_rejects_critical_risk(self, fake_supabase):
        payload = {**LOW_RISK_RULE, "risk_level": "critical"}
        with pytest.raises(ValueError):
            engine.create_rule(payload, created_by="founder@example.com")

    def test_create_rule_rejects_unknown_action(self, fake_supabase):
        payload = {**LOW_RISK_RULE, "actions": [{"action_type": "preis_aendern", "params": {}}]}
        with pytest.raises(ValueError):
            engine.create_rule(payload, created_by="founder@example.com")

    def test_update_rule_bumps_version(self, fake_supabase):
        rule = engine.create_rule(LOW_RISK_RULE, created_by="founder@example.com")
        updated = engine.update_rule(rule["id"], {"name": "Neuer Name"}, updated_by="founder@example.com")
        assert updated["version"] == 2
        assert len(fake_supabase.tables["vt_automation_rule_versions"]) == 2

    def test_update_to_medium_risk_forces_reapproval(self, fake_supabase):
        rule = engine.create_rule(LOW_RISK_RULE, created_by="founder@example.com")
        engine.set_rule_lifecycle_status(rule["id"], status="aktiv", enabled=True, updated_by="founder@example.com")
        updated = engine.update_rule(rule["id"], {"risk_level": "medium", "approval_policy": "always_require_approval"}, updated_by="founder@example.com")
        assert updated["status"] == "entwurf"
        assert updated["enabled"] is False


class TestRuleActivationAndApprovalIntegration:
    def test_low_risk_no_approval_activates_immediately(self, fake_supabase):
        rule = engine.create_rule(LOW_RISK_RULE, created_by="founder@example.com")
        result = engine.request_rule_activation(rule["id"], requested_by="founder@example.com")
        assert result["status"] == "aktiv"
        assert result["enabled"] is True

    def test_medium_risk_creates_approval_and_waits(self, fake_supabase):
        payload = {**LOW_RISK_RULE, "risk_level": "medium", "approval_policy": "always_require_approval", "actions": [{"action_type": "affiliate_produkt_pausieren", "params": {"product_id": "p1"}}]}
        rule = engine.create_rule(payload, created_by="founder@example.com")
        result = engine.request_rule_activation(rule["id"], requested_by="founder@example.com")
        assert result["status"] == "wartet_auf_freigabe"
        approvals = fake_supabase.tables["vt_founder_approvals"]
        assert len(approvals) == 1
        assert approvals[0]["related_entity_type"] == "automation_rule"

    def test_approving_activates_rule_via_shared_approval_center(self, fake_supabase):
        payload = {**LOW_RISK_RULE, "risk_level": "medium", "approval_policy": "always_require_approval", "actions": [{"action_type": "affiliate_produkt_pausieren", "params": {"product_id": "p1"}}]}
        rule = engine.create_rule(payload, created_by="founder@example.com")
        engine.request_rule_activation(rule["id"], requested_by="founder@example.com")
        approval = fake_supabase.tables["vt_founder_approvals"][0]
        admin = SimpleNamespace(email="founder@example.com", role="super_admin")
        founder_approval._apply_entity_side_effect(approval, "freigegeben", admin)
        refreshed = engine.get_rule(rule["id"])
        assert refreshed["status"] == "aktiv"
        assert refreshed["enabled"] is True

    def test_non_super_admin_cannot_activate_medium_risk_rule_via_approval(self, fake_supabase):
        payload = {**LOW_RISK_RULE, "risk_level": "medium", "approval_policy": "always_require_approval", "actions": [{"action_type": "affiliate_produkt_pausieren", "params": {"product_id": "p1"}}]}
        rule = engine.create_rule(payload, created_by="admin@example.com")
        engine.request_rule_activation(rule["id"], requested_by="admin@example.com")
        approval = fake_supabase.tables["vt_founder_approvals"][0]
        non_founder_admin = SimpleNamespace(email="admin@example.com", role="admin")
        founder_approval._apply_entity_side_effect(approval, "freigegeben", non_founder_admin)
        refreshed = engine.get_rule(rule["id"])
        assert refreshed["status"] == "wartet_auf_freigabe"  # unchanged — side effect refused

    def test_rejecting_keeps_rule_in_draft(self, fake_supabase):
        payload = {**LOW_RISK_RULE, "risk_level": "medium", "approval_policy": "always_require_approval", "actions": [{"action_type": "affiliate_produkt_pausieren", "params": {"product_id": "p1"}}]}
        rule = engine.create_rule(payload, created_by="founder@example.com")
        engine.request_rule_activation(rule["id"], requested_by="founder@example.com")
        approval = fake_supabase.tables["vt_founder_approvals"][0]
        admin = SimpleNamespace(email="founder@example.com", role="super_admin")
        founder_approval._apply_entity_side_effect(approval, "abgelehnt", admin)
        refreshed = engine.get_rule(rule["id"])
        assert refreshed["status"] == "entwurf"
        assert refreshed["enabled"] is False


class TestDryRun:
    def test_dry_run_never_mutates(self, fake_supabase):
        rule = engine.create_rule(LOW_RISK_RULE, created_by="founder@example.com")
        engine.set_rule_lifecycle_status(rule["id"], status="aktiv", enabled=True, updated_by="founder@example.com")
        result = engine.dry_run_rule(rule["id"])
        assert result["note"].startswith("Dry Run")
        assert fake_supabase.tables.get("vt_automation_runs", []) == []

    def test_dry_run_reports_action_preview(self, fake_supabase):
        rule = engine.create_rule(LOW_RISK_RULE, created_by="founder@example.com")
        result = engine.dry_run_rule(rule["id"])
        assert result["actions_preview"][0]["action_type"] == "link_pruefen"

    def test_dry_run_missing_rule_raises(self, fake_supabase):
        with pytest.raises(LookupError):
            engine.dry_run_rule("does-not-exist")


class TestManualExecutionAndIdempotency:
    def test_manual_run_creates_run_row(self, fake_supabase):
        fake_supabase.tables["vt_affiliate_products"] = [{"id": "p1", "affiliate_url": "https://example.com", "link_status": "unchecked"}]
        rule = engine.create_rule(LOW_RISK_RULE, created_by="founder@example.com")
        run = engine.run_rule_manually(rule["id"], requested_by="founder@example.com")
        assert run["status"] == "erfolgreich"
        assert len(fake_supabase.tables["vt_automation_runs"]) == 1

    def test_archived_rule_cannot_be_run_manually(self, fake_supabase):
        rule = engine.create_rule(LOW_RISK_RULE, created_by="founder@example.com")
        engine.set_rule_lifecycle_status(rule["id"], status="archiviert", enabled=False, updated_by="founder@example.com")
        with pytest.raises(ValueError):
            engine.run_rule_manually(rule["id"], requested_by="founder@example.com")

    def test_evaluate_and_run_due_rules_is_idempotent_for_same_day(self, fake_supabase):
        fake_supabase.tables["vt_affiliate_products"] = [{"id": "p1", "affiliate_url": "https://example.com", "link_status": "unchecked"}]
        rule = engine.create_rule(LOW_RISK_RULE, created_by="founder@example.com")
        engine.set_rule_lifecycle_status(rule["id"], status="aktiv", enabled=True, updated_by="founder@example.com")
        supabase_rule = engine.get_rule(rule["id"])
        engine.supabase.table("vt_automation_rules").update({"next_run_at": "2000-01-01T00:00:00+00:00"}).eq("id", rule["id"]).execute()

        result_1 = engine.evaluate_and_run_due_rules()
        result_2 = engine.evaluate_and_run_due_rules()
        assert len(result_1["executed"]) == 1
        # second pass either skips (terminal) or is a no-op re-check — never a second run row for the same day
        assert len(fake_supabase.tables["vt_automation_runs"]) == 1


class TestRetryAndDeadLetter:
    def test_failed_action_with_retry_policy_stays_open_for_retry(self, fake_supabase):
        # no matching product => link_pruefen fails
        payload = {**LOW_RISK_RULE, "actions": [{"action_type": "link_pruefen", "params": {"product_id": "missing"}}]}
        rule = engine.create_rule(payload, created_by="founder@example.com")
        run = engine.run_rule_manually(rule["id"], requested_by="founder@example.com")
        assert run["status"] == "fehlgeschlagen_wird_wiederholt"

    def test_max_attempts_reached_creates_dead_letter_task_and_alert(self, fake_supabase):
        payload = {**LOW_RISK_RULE, "retry_policy": {"type": "fixed", "max_attempts": 1, "cooldown_seconds": 0}, "actions": [{"action_type": "link_pruefen", "params": {"product_id": "missing"}}]}
        rule = engine.create_rule(payload, created_by="founder@example.com")
        run = engine.run_rule_manually(rule["id"], requested_by="founder@example.com")
        assert run["status"] == "dead_letter"
        assert len(fake_supabase.tables["vt_automation_dead_letters"]) == 1
        assert len(fake_supabase.tables["vt_founder_tasks"]) == 1
        assert len(fake_supabase.tables["vt_automation_alerts"]) == 1


class TestRollback:
    def test_reversible_action_can_be_rolled_back(self, fake_supabase):
        fake_supabase.tables["vt_affiliate_products"] = [{"id": "p1", "status": "active"}]
        payload = {**LOW_RISK_RULE, "risk_level": "medium", "actions": [{"action_type": "affiliate_produkt_pausieren", "params": {"product_id": "p1"}}]}
        rule = engine.create_rule(payload, created_by="founder@example.com")
        run = engine.run_rule_manually(rule["id"], requested_by="founder@example.com")
        assert run["status"] == "erfolgreich"
        assert fake_supabase.tables["vt_affiliate_products"][0]["status"] == "paused"

        result = engine.rollback_run(run["id"], requested_by="founder@example.com")
        assert result["status"] == "zurueckgerollt"
        assert fake_supabase.tables["vt_affiliate_products"][0]["status"] == "active"

    def test_cannot_rollback_twice(self, fake_supabase):
        fake_supabase.tables["vt_affiliate_products"] = [{"id": "p1", "status": "active"}]
        payload = {**LOW_RISK_RULE, "risk_level": "medium", "actions": [{"action_type": "affiliate_produkt_pausieren", "params": {"product_id": "p1"}}]}
        rule = engine.create_rule(payload, created_by="founder@example.com")
        run = engine.run_rule_manually(rule["id"], requested_by="founder@example.com")
        engine.rollback_run(run["id"], requested_by="founder@example.com")
        with pytest.raises(ValueError):
            engine.rollback_run(run["id"], requested_by="founder@example.com")

    def test_non_reversible_action_cannot_be_rolled_back(self, fake_supabase):
        fake_supabase.tables["vt_affiliate_products"] = [{"id": "p1", "affiliate_url": "https://example.com"}]
        rule = engine.create_rule(LOW_RISK_RULE, created_by="founder@example.com")
        run = engine.run_rule_manually(rule["id"], requested_by="founder@example.com")
        with pytest.raises(ValueError):
            engine.rollback_run(run["id"], requested_by="founder@example.com")


class TestTaskAndAlertIntegration:
    def test_task_erstellen_action_is_idempotent(self, fake_supabase):
        payload = {**LOW_RISK_RULE, "actions": [{"action_type": "task_erstellen", "params": {"dedupe_key": "fixed_key", "title": "X", "category": "technik", "reason": "r"}}]}
        rule = engine.create_rule(payload, created_by="founder@example.com")
        engine.run_rule_manually(rule["id"], requested_by="founder@example.com")
        engine.set_rule_lifecycle_status(rule["id"], status="entwurf", enabled=False, updated_by="x")  # allow re-run
        engine._execute_action(payload["actions"][0], rule)
        assert len(fake_supabase.tables["vt_founder_tasks"]) == 1


class TestOpportunityDetection:
    def test_repeated_approvals_create_opportunity(self, fake_supabase):
        from datetime import datetime, timezone
        now_iso = datetime.now(timezone.utc).isoformat()
        fake_supabase.tables["vt_founder_approvals"] = [
            {"source": "affiliate_produkte", "category": "affiliate", "status": "freigegeben", "decided_at": now_iso} for _ in range(5)
        ]
        opportunity_detector.run_opportunity_detection()
        opportunities = fake_supabase.tables["vt_automation_opportunities"]
        assert len(opportunities) == 1
        assert opportunities[0]["occurrences"] == 5

    def test_below_threshold_creates_no_opportunity(self, fake_supabase):
        from datetime import datetime, timezone
        now_iso = datetime.now(timezone.utc).isoformat()
        fake_supabase.tables["vt_founder_approvals"] = [
            {"source": "affiliate_produkte", "category": "affiliate", "status": "freigegeben", "decided_at": now_iso} for _ in range(2)
        ]
        opportunity_detector.run_opportunity_detection()
        assert fake_supabase.tables.get("vt_automation_opportunities", []) == []

    def test_dismissed_opportunity_never_recreated(self, fake_supabase):
        from datetime import datetime, timezone
        now_iso = datetime.now(timezone.utc).isoformat()
        fake_supabase.tables["vt_founder_approvals"] = [
            {"source": "affiliate_produkte", "category": "affiliate", "status": "freigegeben", "decided_at": now_iso} for _ in range(5)
        ]
        opportunity_detector.run_opportunity_detection()
        opp_id = fake_supabase.tables["vt_automation_opportunities"][0]["id"]
        opportunity_detector.dismiss_opportunity(opp_id)
        opportunity_detector.run_opportunity_detection()
        assert fake_supabase.tables["vt_automation_opportunities"][0]["status"] == "abgelehnt"


class TestAutomationScore:
    def test_score_is_none_with_no_data(self, fake_supabase):
        result = score_module.compute_automation_score()
        assert result["overall_percentage"] is None

    def test_score_computed_from_real_runs(self, fake_supabase):
        from datetime import datetime, timezone
        now_iso = datetime.now(timezone.utc).isoformat()
        fake_supabase.tables["vt_automation_runs"] = [{"status": "erfolgreich", "created_at": now_iso} for _ in range(3)]
        result = score_module.compute_automation_score()
        assert result["overall_percentage"] == 100
        assert result["automated_runs_30d"] == 3


class TestRouterPermissions:
    def test_dashboard_requires_view_automation_engine(self, fake_supabase, permission_spy):
        import asyncio
        asyncio.run(automation_router.automation_dashboard(authorization="Bearer x"))
        assert permission_spy[-1] == ("Bearer x", "view_automation_engine")

    def test_create_rule_requires_manage_automation_engine(self, fake_supabase, permission_spy):
        import asyncio
        data = automation_router.RuleInput(**LOW_RISK_RULE)
        asyncio.run(automation_router.create_rule(data, authorization="Bearer x"))
        assert permission_spy[-1] == ("Bearer x", "manage_automation_engine")

    def test_admin_role_denied_automation_permissions_by_default(self):
        # "admin" auto-grants everything except manage_roles/manage_security/
        # view_automation_engine/manage_automation_engine — per spec, normal
        # admins must NOT get automatic access to the Automation Engine.
        assert "view_automation_engine" not in admin_rbac.ROLE_PERMISSIONS["admin"]
        assert "manage_automation_engine" not in admin_rbac.ROLE_PERMISSIONS["admin"]

    def test_automation_manager_role_has_exactly_the_two_permissions(self):
        assert admin_rbac.ROLE_PERMISSIONS["automation_manager"] == {"view_automation_engine", "manage_automation_engine"}

    def test_analyst_has_view_only(self):
        assert "view_automation_engine" in admin_rbac.ROLE_PERMISSIONS["analyst"]
        assert "manage_automation_engine" not in admin_rbac.ROLE_PERMISSIONS["analyst"]

    def test_super_admin_has_both(self):
        assert "view_automation_engine" in admin_rbac.ROLE_PERMISSIONS["super_admin"]
        assert "manage_automation_engine" in admin_rbac.ROLE_PERMISSIONS["super_admin"]


class TestRouterBehavior:
    @pytest.mark.anyio
    async def test_rule_creation_via_router_returns_disabled_draft(self, fake_supabase, permission_spy):
        data = automation_router.RuleInput(**LOW_RISK_RULE)
        result = await automation_router.create_rule(data, authorization="Bearer x")
        assert result["enabled"] is False
        assert result["status"] == "entwurf"

    @pytest.mark.anyio
    async def test_router_rejects_unknown_action_with_400(self, fake_supabase, permission_spy):
        payload = {**LOW_RISK_RULE, "actions": [{"action_type": "preis_aendern", "params": {}}]}
        data = automation_router.RuleInput(**payload)
        with pytest.raises(HTTPException) as exc_info:
            await automation_router.create_rule(data, authorization="Bearer x")
        assert exc_info.value.status_code == 400

    @pytest.mark.anyio
    async def test_get_registry_lists_no_critical_actions(self, fake_supabase, permission_spy):
        result = await automation_router.get_registry(authorization="Bearer x")
        assert all(a["risk_level"] != "critical" for a in result["actions"])

    @pytest.mark.anyio
    async def test_dry_run_endpoint_via_router(self, fake_supabase, permission_spy):
        data = automation_router.RuleInput(**LOW_RISK_RULE)
        rule = await automation_router.create_rule(data, authorization="Bearer x")
        result = await automation_router.dry_run_rule(rule["id"], authorization="Bearer x")
        assert result["note"].startswith("Dry Run")

    @pytest.mark.anyio
    async def test_automation_score_endpoint(self, fake_supabase, permission_spy):
        result = await automation_router.automation_score_endpoint(authorization="Bearer x")
        assert "overall_percentage" in result

    @pytest.mark.anyio
    async def test_cost_control_is_honest_about_no_token_tracking(self, fake_supabase, permission_spy):
        result = await automation_router.cost_control(authorization="Bearer x")
        assert result["token_usage"] is None


class TestDailyBriefingIntegration:
    def test_get_daily_briefing_summary_never_raises_without_data(self, fake_supabase):
        summary = engine.get_daily_briefing_summary()
        assert summary["auto_completed_today"] == 0
        assert summary["failed_today"] == 0
