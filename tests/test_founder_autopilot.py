"""Tests for Founder Autopilot (VitalTwin Enterprise, Founder Operating
System, Submodule J): mode/kill-switch/incident state machine, policies
(validation of always-manual categories, versioning), event synthesis,
priority engine & attention score, alerts, module health, release
readiness (never falsely 'bereit'), automation score roll-up, work-saved
estimate, orchestrator (today view, decision inbox, one-click approval
safety rules, orchestration cycle gating), and the API router —
permissions (super_admin-only manage, executive_analyst read-only, admin
excluded), AI Q&A (mocked provider)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core import autopilot_alerts as alerts_module
from app.core import autopilot_events as events_module
from app.core import autopilot_module_health as health_module
from app.core import autopilot_orchestrator as orchestrator
from app.core import autopilot_planning as planning_module
from app.core import autopilot_policies as policies_module
from app.core import autopilot_priority as priority_module
from app.core import autopilot_release_readiness as readiness_module
from app.core import autopilot_score as score_module
from app.core import autopilot_state as state_module
from app.core.admin_rbac import ROLE_PERMISSIONS, role_has_permission
from app.routers import founder_autopilot as autopilot_router
from app.services.ai_provider import AIProviderUnavailableError


@pytest.fixture
def anyio_backend():
    return "asyncio"


class _FakeQuery:
    def __init__(self, table_rows: list[dict]):
        self._table_rows = table_rows
        self._predicates = []
        self._pending_insert = None
        self._pending_update = None
        self._limit_n = None
        self._order_field = None
        self._order_desc = False

    def select(self, *a, count=None, **k):
        return self

    def eq(self, field, value):
        self._predicates.append(lambda row, f=field, v=value: row.get(f) == v)
        return self

    def gte(self, field, value):
        self._predicates.append(lambda row, f=field, v=value: str(row.get(f, "")) >= str(value))
        return self

    def lt(self, field, value):
        self._predicates.append(lambda row, f=field, v=value: str(row.get(f, "")) < str(value))
        return self

    def in_(self, field, values):
        self._predicates.append(lambda row, f=field, v=set(values): row.get(f) in v)
        return self

    def order(self, field, desc=False, **k):
        self._order_field = field
        self._order_desc = desc
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
        if self._order_field:
            rows = sorted(rows, key=lambda r: str(r.get(self._order_field, "")), reverse=self._order_desc)
        if self._limit_n is not None:
            rows = rows[: self._limit_n]
        return rows

    def execute(self):
        if self._pending_insert is not None:
            new_row = dict(self._pending_insert)
            new_row.setdefault("id", f"id-{len(self._table_rows) + 1}")
            new_row.setdefault("created_at", f"{len(self._table_rows):010d}")
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
def autopilot_supabase(monkeypatch):
    fake = _FakeSupabase()
    monkeypatch.setattr(state_module, "supabase", fake)
    monkeypatch.setattr(policies_module, "supabase", fake)
    monkeypatch.setattr(events_module, "supabase", fake)
    monkeypatch.setattr(alerts_module, "supabase", fake)
    monkeypatch.setattr(health_module, "supabase", fake)
    monkeypatch.setattr(readiness_module, "supabase", fake)
    monkeypatch.setattr(orchestrator, "supabase", fake)
    monkeypatch.setattr(autopilot_router, "supabase", fake)
    import app.core.automation_score as g_score
    import app.core.documentation_score as i_score
    import app.core.executive_risk_opportunity as h_risk_opp
    import app.core.founder_business_metrics as biz_metrics
    monkeypatch.setattr(g_score, "supabase", fake)
    monkeypatch.setattr(i_score, "supabase", fake)
    monkeypatch.setattr(h_risk_opp, "supabase", fake)
    monkeypatch.setattr(biz_metrics, "supabase", fake)
    return fake


@pytest.fixture
def autopilot_permission_spy(monkeypatch):
    calls: list[tuple] = []

    def _fake(authorization, permission):
        calls.append((authorization, permission))
        return SimpleNamespace(email="founder@example.com", role="super_admin")

    monkeypatch.setattr(autopilot_router, "require_admin_permission", _fake)
    return calls


class TestRbacMatrix:
    def test_only_super_admin_can_manage_autopilot(self):
        managers = {r for r, p in ROLE_PERMISSIONS.items() if "manage_founder_autopilot" in p}
        assert managers == {"super_admin"}

    def test_executive_analyst_can_read_autopilot(self):
        assert role_has_permission("executive_analyst", "view_founder_autopilot")
        assert not role_has_permission("executive_analyst", "manage_founder_autopilot")

    def test_admin_excluded_from_autopilot(self):
        assert not role_has_permission("admin", "view_founder_autopilot")
        assert not role_has_permission("admin", "manage_founder_autopilot")


class TestAutopilotState:
    def test_default_state_is_assist_mode(self, autopilot_supabase):
        state = state_module.get_current_state()
        assert state["mode"] == "assist"

    def test_set_mode_rejects_invalid_mode(self, autopilot_supabase):
        with pytest.raises(ValueError):
            state_module.set_mode("turbo", reason=None, changed_by="x")

    def test_set_mode_appends_new_state_row(self, autopilot_supabase):
        state_module.set_mode("monitor", reason="testing", changed_by="founder@example.com")
        assert state_module.get_current_state()["mode"] == "monitor"

    def test_kill_switch_activation_blocks_all_categories(self, autopilot_supabase):
        state_module.activate_kill_switch(reason="emergency", activated_by="founder@example.com")
        assert state_module.allowed_categories_for_current_state() == frozenset()

    def test_kill_switch_deactivation_restores_previous_mode(self, autopilot_supabase):
        state_module.set_mode("controlled_autopilot", reason=None, changed_by="x")
        state_module.activate_kill_switch(reason="emergency", activated_by="x")
        state_module.deactivate_kill_switch(deactivated_by="x")
        state = state_module.get_current_state()
        assert state["kill_switch_active"] is False
        assert state["mode"] == "controlled_autopilot"

    def test_assist_mode_never_auto_executes(self, autopilot_supabase):
        state_module.set_mode("assist", reason=None, changed_by="x")
        assert state_module.allowed_categories_for_current_state() == frozenset()

    def test_controlled_autopilot_allows_safe_categories_only(self, autopilot_supabase):
        state_module.set_mode("controlled_autopilot", reason=None, changed_by="x")
        allowed = state_module.allowed_categories_for_current_state()
        assert "affiliate" in allowed
        assert "sicherheit" not in allowed

    def test_incident_activation_sets_incident_mode(self, autopilot_supabase):
        state_module.activate_incident_mode(title="DB down", reason="Database issue", activated_by="founder@example.com")
        state = state_module.get_current_state()
        assert state["mode"] == "incident_mode"
        assert state["incident_mode_active"] is True

    def test_incident_resolution_restores_assist(self, autopilot_supabase):
        result = state_module.activate_incident_mode(title="DB down", reason="x", activated_by="x")
        state_module.resolve_incident(result["id"], resolved_by="x")
        assert state_module.get_current_state()["mode"] == "assist"


class TestAutopilotPolicies:
    def test_policy_rejects_critical_risk_level(self, autopilot_supabase):
        with pytest.raises(ValueError):
            policies_module.create_policy({"mode": "assist", "maximum_risk_level": "critical", "allowed_categories": []}, created_by="x")

    def test_policy_rejects_always_manual_category(self, autopilot_supabase):
        with pytest.raises(ValueError):
            policies_module.create_policy({"mode": "controlled_autopilot", "maximum_risk_level": "low", "allowed_categories": ["preise"]}, created_by="x")

    def test_policy_created_disabled_by_default(self, autopilot_supabase):
        policy = policies_module.create_policy({"mode": "assist", "maximum_risk_level": "low", "allowed_categories": ["affiliate"]}, created_by="x")
        assert policy["enabled"] is False
        assert policy["status"] == "entwurf"

    def test_update_policy_bumps_version_and_resets_to_draft(self, autopilot_supabase):
        policy = policies_module.create_policy({"mode": "assist", "maximum_risk_level": "low", "allowed_categories": ["affiliate"]}, created_by="x")
        policies_module.activate_policy(policy["id"], activated_by="x")
        updated = policies_module.update_policy(policy["id"], {"description": "changed"}, updated_by="x")
        assert updated["version"] == 2
        assert updated["status"] == "entwurf"
        assert updated["enabled"] is False

    def test_update_policy_stores_previous_version_snapshot(self, autopilot_supabase):
        policy = policies_module.create_policy({"mode": "assist", "maximum_risk_level": "low", "allowed_categories": ["affiliate"]}, created_by="x")
        updated = policies_module.update_policy(policy["id"], {"description": "v2"}, updated_by="x")
        assert len(updated["previous_versions"]) == 1

    def test_effective_categories_only_from_active_enabled_policies(self, autopilot_supabase):
        policy = policies_module.create_policy({"mode": "controlled_autopilot", "maximum_risk_level": "low", "allowed_categories": ["affiliate", "business"]}, created_by="x")
        assert policies_module.effective_allowed_categories() == frozenset()
        policies_module.activate_policy(policy["id"], activated_by="x")
        assert "affiliate" in policies_module.effective_allowed_categories()


class TestPriorityAndAttentionScore:
    def test_critical_severity_yields_kritisch_priority(self):
        assert priority_module.compute_priority({"severity": "kritisch"}) == "kritisch"

    def test_legal_category_boosts_priority(self):
        low = priority_module.compute_priority({"severity": "niedrig", "category": "sonstiges"})
        legal = priority_module.compute_priority({"severity": "niedrig", "category": "rechtliches"})
        assert priority_module.PRIORITY_LEVELS.index(legal) <= priority_module.PRIORITY_LEVELS.index(low)

    def test_revenue_alone_does_not_dominate_priority(self):
        # Revenue-only factor must never out-rank a security/legal factor.
        revenue_score = priority_module.compute_priority_score({"revenue_impact": 1.0})
        legal_score = priority_module.compute_priority_score({"legal_risk": 1.0})
        assert legal_score > revenue_score

    def test_attention_score_boosts_irreversible_items(self):
        reversible = priority_module.compute_attention_score({"severity": "mittel", "reversible": True})
        irreversible = priority_module.compute_attention_score({"severity": "mittel", "reversible": False})
        assert irreversible > reversible

    def test_attention_score_boosts_legal_category(self):
        normal = priority_module.compute_attention_score({"severity": "mittel", "category": "affiliate"})
        legal = priority_module.compute_attention_score({"severity": "mittel", "category": "rechtliches"})
        assert legal > normal


class TestAlerts:
    def test_repeated_automation_failures_creates_alert(self, autopilot_supabase):
        autopilot_supabase.tables["vt_automation_dead_letters"] = [{"id": "1"}, {"id": "2"}, {"id": "3"}]
        alerts_module._detect_repeated_automation_failures()
        assert len(autopilot_supabase.tables["vt_founder_autopilot_alerts"]) == 1

    def test_alert_deduplicated_on_rerun(self, autopilot_supabase):
        autopilot_supabase.tables["vt_automation_dead_letters"] = [{"id": "1"}, {"id": "2"}, {"id": "3"}]
        alerts_module._detect_repeated_automation_failures()
        alerts_module._detect_repeated_automation_failures()
        assert len(autopilot_supabase.tables["vt_founder_autopilot_alerts"]) == 1

    def test_closed_alert_never_reopens(self, autopilot_supabase):
        autopilot_supabase.tables["vt_automation_dead_letters"] = [{"id": "1"}, {"id": "2"}, {"id": "3"}]
        alerts_module._detect_repeated_automation_failures()
        alert_id = autopilot_supabase.tables["vt_founder_autopilot_alerts"][0]["id"]
        alerts_module.close_alert(alert_id)
        alerts_module._detect_repeated_automation_failures()
        assert autopilot_supabase.tables["vt_founder_autopilot_alerts"][0]["status"] == "archiviert"

    def test_escalate_sets_critical_severity(self, autopilot_supabase):
        autopilot_supabase.tables["vt_founder_autopilot_alerts"] = [{"id": "a1", "severity": "mittel", "status": "offen"}]
        alerts_module.escalate_alert("a1")
        assert autopilot_supabase.tables["vt_founder_autopilot_alerts"][0]["severity"] == "kritisch"
        assert autopilot_supabase.tables["vt_founder_autopilot_alerts"][0]["escalated"] is True


class TestModuleHealth:
    def test_returns_all_nine_submodules(self, autopilot_supabase):
        results = health_module.compute_module_health()
        assert {r["module"] for r in results} == {"A", "B", "C", "D", "E", "F", "G", "H", "I"}

    def test_every_status_has_a_reason(self, autopilot_supabase):
        for result in health_module.compute_module_health():
            assert result["reason"]

    def test_many_open_tasks_triggers_warning(self, autopilot_supabase):
        autopilot_supabase.tables["vt_founder_tasks"] = [{"status": "neu"}] * 25
        results = health_module.compute_module_health()
        task_module = next(r for r in results if r["module"] == "C")
        assert task_module["status"] == "warning"


class TestReleaseReadiness:
    def test_never_bereit_when_unverifiable_checks_exist(self, autopilot_supabase):
        result = readiness_module.compute_release_readiness()
        assert result["verdict"] != "bereit"  # TS/Lint/Tests/Build are always unverifiable here

    def test_critical_bug_forces_nicht_bereit(self, autopilot_supabase):
        autopilot_supabase.tables["vt_founder_tasks"] = [{"priority": "kritisch", "status": "neu"}]
        result = readiness_module.compute_release_readiness()
        assert result["verdict"] == "nicht_bereit"

    def test_no_check_claims_typescript_passed(self, autopilot_supabase):
        result = readiness_module.compute_release_readiness()
        ts_check = next(c for c in result["checks"] if c["name"] == "TypeScript")
        assert ts_check["verifiable"] is False
        assert ts_check["passed"] is None

    def test_autopilot_never_publishes_itself(self, autopilot_supabase):
        result = readiness_module.compute_release_readiness()
        assert "niemals selbst" in result["note"]


class TestAutomationScoreAndWorkSaved:
    def test_founder_os_score_combines_g_and_i(self, autopilot_supabase):
        autopilot_supabase.tables["vt_automation_runs"] = [{"status": "erfolgreich"}] * 5
        autopilot_supabase.tables["vt_documentation_registry"] = [{"is_generated": True, "requires_approval": False}] * 3
        result = score_module.compute_founder_os_automation_score()
        assert result["per_submodule"]["G_automation_engine"] is not None
        assert result["per_submodule"]["I_auto_documentation"] is not None

    def test_work_saved_is_labeled_as_estimate(self, autopilot_supabase):
        autopilot_supabase.tables["vt_automation_runs"] = [{"status": "erfolgreich"}] * 10
        result = score_module.compute_work_saved_estimate()
        assert result["uncertainty"] == "hoch"
        assert "Schätzung" in result["note"] or result["automated_operations_30d"] == 0


class TestOrchestrator:
    def test_kill_switch_blocks_orchestration_cycle(self, autopilot_supabase, monkeypatch):
        monkeypatch.setattr(state_module, "supabase", autopilot_supabase)
        state_module.activate_kill_switch(reason="test", activated_by="x")
        result = orchestrator.run_orchestration_cycle(triggered_by="x")
        assert result["executed"] is False
        assert result["status"] == "gestoppt"

    def test_monitor_mode_never_executes(self, autopilot_supabase, monkeypatch):
        monkeypatch.setattr(state_module, "supabase", autopilot_supabase)
        state_module.set_mode("monitor", reason=None, changed_by="x")
        result = orchestrator.run_orchestration_cycle(triggered_by="x")
        assert result["executed"] is False

    def test_assist_mode_prepares_but_never_executes(self, autopilot_supabase, monkeypatch):
        monkeypatch.setattr(state_module, "supabase", autopilot_supabase)
        state_module.set_mode("assist", reason=None, changed_by="x")
        result = orchestrator.run_orchestration_cycle(triggered_by="x")
        assert result["executed"] is False
        assert result["status"] == "vorbereitet"

    def test_bulk_approval_rejects_mixed_categories(self, autopilot_supabase):
        autopilot_supabase.tables["vt_founder_approvals"] = [
            {"id": "1", "category": "affiliate", "priority": "niedrig", "status": "neu"},
            {"id": "2", "category": "business", "priority": "niedrig", "status": "neu"},
        ]
        with pytest.raises(ValueError):
            orchestrator.execute_one_click_approval(["1", "2"], decided_by="x")

    def test_bulk_approval_rejects_high_priority(self, autopilot_supabase):
        autopilot_supabase.tables["vt_founder_approvals"] = [{"id": "1", "category": "affiliate", "priority": "kritisch", "status": "neu"}]
        with pytest.raises(ValueError):
            orchestrator.execute_one_click_approval(["1"], decided_by="x")

    def test_bulk_approval_rejects_excluded_category(self, autopilot_supabase):
        autopilot_supabase.tables["vt_founder_approvals"] = [{"id": "1", "category": "preise", "priority": "niedrig", "status": "neu"}]
        with pytest.raises(ValueError):
            orchestrator.execute_one_click_approval(["1"], decided_by="x")

    def test_bulk_approval_rejects_partner_activation(self, autopilot_supabase):
        autopilot_supabase.tables["vt_founder_approvals"] = [
            {"id": "1", "category": "affiliate", "priority": "niedrig", "status": "neu", "related_entity_type": "affiliate_partner"},
        ]
        with pytest.raises(ValueError):
            orchestrator.execute_one_click_approval(["1"], decided_by="x")

    def test_bulk_approval_succeeds_for_safe_items(self, autopilot_supabase):
        autopilot_supabase.tables["vt_founder_approvals"] = [
            {"id": "1", "category": "affiliate", "priority": "niedrig", "status": "neu", "related_entity_type": None},
            {"id": "2", "category": "affiliate", "priority": "niedrig", "status": "neu", "related_entity_type": None},
        ]
        result = orchestrator.execute_one_click_approval(["1", "2"], decided_by="founder@example.com")
        assert result["updated"] == 2
        assert all(a["status"] == "freigegeben" for a in autopilot_supabase.tables["vt_founder_approvals"])


class TestDailyPlanCaps:
    def test_daily_plan_never_exceeds_three_per_section(self, autopilot_supabase, monkeypatch):
        monkeypatch.setattr(planning_module, "supabase", autopilot_supabase)
        autopilot_supabase.tables["vt_founder_tasks"] = [{"id": str(i), "status": "neu", "priority": "hoch"} for i in range(10)]
        plan = planning_module.compute_daily_plan()
        assert len(plan["important_tasks"]) <= 3
        assert len(plan["critical_decisions"]) <= 3


class TestAutopilotRouterPermissions:
    @pytest.mark.anyio
    async def test_today_view_requires_view_permission(self, autopilot_supabase, autopilot_permission_spy):
        await autopilot_router.today_view(authorization="Bearer x")
        assert autopilot_permission_spy[-1] == ("Bearer x", "view_founder_autopilot")

    @pytest.mark.anyio
    async def test_set_mode_requires_manage_permission(self, autopilot_supabase, autopilot_permission_spy):
        data = autopilot_router.ModeInput(mode="monitor")
        await autopilot_router.set_mode(data, authorization="Bearer x")
        assert autopilot_permission_spy[-1] == ("Bearer x", "manage_founder_autopilot")

    @pytest.mark.anyio
    async def test_kill_switch_requires_manage_permission(self, autopilot_supabase, autopilot_permission_spy):
        data = autopilot_router.KillSwitchInput(reason="test")
        await autopilot_router.activate_kill_switch(data, authorization="Bearer x")
        assert autopilot_permission_spy[-1] == ("Bearer x", "manage_founder_autopilot")

    @pytest.mark.anyio
    async def test_bulk_approve_rejects_invalid_selection_with_400(self, autopilot_supabase, autopilot_permission_spy):
        autopilot_supabase.tables["vt_founder_approvals"] = [{"id": "1", "category": "preise", "priority": "niedrig", "status": "neu"}]
        data = autopilot_router.BulkApprovalInput(approval_ids=["1"])
        with pytest.raises(HTTPException) as exc_info:
            await autopilot_router.bulk_approve(data, authorization="Bearer x")
        assert exc_info.value.status_code == 400


class TestAskAutopilot:
    @pytest.mark.anyio
    async def test_provider_failure_returns_503(self, autopilot_supabase, autopilot_permission_spy, monkeypatch):
        monkeypatch.setattr(orchestrator, "get_today_view", lambda: {"auto_completed_today": 3, "failed_automations_today": 0, "waiting_approvals": 1, "entries": []})

        class _FailingProvider:
            async def generate_recommendation_explanation(self, *, system_prompt, context_text):
                raise AIProviderUnavailableError("down")

        monkeypatch.setattr(autopilot_router, "_get_ai_provider", lambda: _FailingProvider())
        data = autopilot_router.AskInput(question="Was muss ich heute entscheiden?")
        with pytest.raises(HTTPException) as exc_info:
            await autopilot_router.ask_autopilot(data, request=SimpleNamespace(client=SimpleNamespace(host="127.0.0.40")), authorization="Bearer x")
        assert exc_info.value.status_code == 503

    @pytest.mark.anyio
    async def test_successful_answer_is_grounded(self, autopilot_supabase, autopilot_permission_spy, monkeypatch):
        monkeypatch.setattr(orchestrator, "get_today_view", lambda: {"auto_completed_today": 3, "failed_automations_today": 0, "waiting_approvals": 1, "entries": []})

        class _FakeProvider:
            async def generate_recommendation_explanation(self, *, system_prompt, context_text):
                assert "Frage:" in context_text
                return "Zusammenfassung aus echten Founder-OS-Daten."

        monkeypatch.setattr(autopilot_router, "_get_ai_provider", lambda: _FakeProvider())
        data = autopilot_router.AskInput(question="Was muss ich heute entscheiden?")
        result = await autopilot_router.ask_autopilot(data, request=SimpleNamespace(client=SimpleNamespace(host="127.0.0.41")), authorization="Bearer x")
        assert result["insufficient_data"] is False
        assert "Founder-OS-Daten" in result["answer"]
