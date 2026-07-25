"""Tests for CEO Intelligence (VitalTwin Enterprise, Founder Operating
System, Submodule H): executive metrics/scorecard/goals/forecast, risk &
opportunity aggregation (dedup via source table+id, no parallel storage),
scenario planning (computable vs. honestly not-computable), executive
summary, and the API router — permissions (incl. executive_analyst
read-only, admin excluded), AI Q&A (mocked provider), export."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core import executive_goals
from app.core import executive_metrics as ex_metrics
from app.core import executive_risk_opportunity as risk_opp
from app.core import executive_scenarios
from app.core import executive_scorecard
from app.core import executive_summary
from app.core.admin_rbac import ROLE_PERMISSIONS, role_has_permission
from app.routers import founder_ceo_intelligence as ceo_router
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
        self._pending_delete = False
        self._limit_n = None
        self._count_mode = False

    def select(self, *a, count=None, **k):
        if count:
            self._count_mode = True
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

    def delete(self):
        self._pending_delete = True
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
        if self._pending_delete:
            matched = [r for r in self._table_rows if all(p(r) for p in self._predicates)]
            for row in matched:
                self._table_rows.remove(row)
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
def ceo_supabase(monkeypatch):
    fake = _FakeSupabase()
    monkeypatch.setattr(ex_metrics, "supabase", fake)
    monkeypatch.setattr(risk_opp, "supabase", fake)
    monkeypatch.setattr(executive_goals, "supabase", fake)
    monkeypatch.setattr(executive_scenarios, "supabase", fake)
    monkeypatch.setattr(ceo_router, "supabase", fake)
    import app.core.founder_business_metrics as biz_metrics
    import app.core.automation_engine as auto_engine
    import app.core.automation_score as auto_score
    monkeypatch.setattr(biz_metrics, "supabase", fake)
    monkeypatch.setattr(auto_engine, "supabase", fake)
    monkeypatch.setattr(auto_score, "supabase", fake)
    return fake


@pytest.fixture
def ceo_permission_spy(monkeypatch):
    calls: list[tuple] = []

    def _fake(authorization, permission):
        calls.append((authorization, permission))
        return SimpleNamespace(email="founder@example.com", role="super_admin")

    monkeypatch.setattr(ceo_router, "require_admin_permission", _fake)
    return calls


class TestRbacMatrix:
    def test_executive_analyst_is_read_only(self):
        assert ROLE_PERMISSIONS["executive_analyst"] == {"view_ceo_intelligence"}

    def test_admin_excluded_from_ceo_intelligence(self):
        assert not role_has_permission("admin", "view_ceo_intelligence")
        assert not role_has_permission("admin", "manage_ceo_intelligence")

    def test_super_admin_has_ceo_intelligence(self):
        assert role_has_permission("super_admin", "view_ceo_intelligence")
        assert role_has_permission("super_admin", "manage_ceo_intelligence")

    def test_no_other_role_can_manage(self):
        managers = {r for r, p in ROLE_PERMISSIONS.items() if "manage_ceo_intelligence" in p}
        assert managers == {"super_admin"}


class TestExecutiveMetrics:
    def test_overview_never_fabricates_revenue(self, ceo_supabase):
        overview = ex_metrics.get_ceo_overview()
        assert overview["revenue_today"]["value"] is None
        assert overview["revenue_today"]["note"]

    def test_overview_computes_real_open_decisions(self, ceo_supabase):
        ceo_supabase.tables["vt_founder_tasks"] = [{"status": "neu"}, {"status": "erledigt"}]
        ceo_supabase.tables["vt_founder_approvals"] = [{"status": "neu"}]
        overview = ex_metrics.get_ceo_overview()
        assert overview["open_founder_decisions"]["value"] == 2

    def test_missing_data_has_nicht_verbunden_quality(self, ceo_supabase):
        overview = ex_metrics.get_ceo_overview()
        assert overview["product_status"]["data_quality"] == "nicht_verbunden"

    def test_strategic_kpis_groups_present(self, ceo_supabase):
        kpis = ex_metrics.get_strategic_kpis()
        for group in ("nutzer", "business", "premium", "affiliate", "ki", "produkt", "technik", "automatisierung"):
            assert group in kpis


class TestExecutiveScorecard:
    def test_returns_fourteen_dimensions(self, ceo_supabase):
        items = executive_scorecard.compute_scorecard()
        assert len(items) == 14

    def test_no_data_dimension_has_keine_daten_status(self, ceo_supabase):
        items = executive_scorecard.compute_scorecard()
        revenue = next(i for i in items if i["area"] == "Umsatz")
        assert revenue["status"] == "keine_daten"

    def test_every_dimension_has_next_action(self, ceo_supabase):
        for item in executive_scorecard.compute_scorecard():
            assert item["next_action"]


class TestExecutiveGoalsForecast:
    def test_forecast_not_computable_without_dates(self):
        goal = {"target_value": 100, "start_value": 0, "start_date": None}
        forecast = executive_goals.forecast_goal(goal, current_value=50)
        assert forecast["computable"] is False

    def test_forecast_computable_with_full_data(self):
        import datetime
        start = (datetime.date.today() - datetime.timedelta(days=10)).isoformat()
        target = (datetime.date.today() + datetime.timedelta(days=10)).isoformat()
        goal = {"target_value": 100, "start_value": 0, "start_date": start, "target_date": target}
        forecast = executive_goals.forecast_goal(goal, current_value=50)
        assert forecast["computable"] is True
        assert "Bei gleichbleibender Entwicklung" in forecast["statement"]

    def test_forecast_never_guarantees(self):
        import datetime
        start = (datetime.date.today() - datetime.timedelta(days=10)).isoformat()
        goal = {"target_value": 100, "start_value": 0, "start_date": start, "target_date": None}
        forecast = executive_goals.forecast_goal(goal, current_value=50)
        assert "garantiert" not in (forecast["statement"] or "").lower()

    def test_list_strategic_goals_includes_forecast(self, ceo_supabase):
        ceo_supabase.tables["vt_founder_business_goals"] = [
            {"id": "g1", "category": "individuell", "target_value": 100, "start_value": 0, "start_date": None, "title": "X"},
        ]
        goals = executive_goals.list_strategic_goals()
        assert "forecast" in goals[0]


class TestExecutiveRiskOpportunityAggregation:
    def test_risks_aggregate_from_insights_and_alerts(self, ceo_supabase):
        ceo_supabase.tables["vt_founder_business_insights"] = [
            {"id": "i1", "title": "Risiko A", "category": "umsatzrisiko", "status": "erkannt"},
        ]
        ceo_supabase.tables["vt_automation_alerts"] = [{"id": "a1", "title": "Alert A", "status": "offen", "severity": "hoch", "message": "m"}]
        ceo_supabase.tables["vt_affiliate_products"] = []
        risks = risk_opp.list_executive_risks()
        refs = {r["ref"] for r in risks}
        assert "insight:i1" in refs
        assert "alert:a1" in refs

    def test_terminal_insights_excluded(self, ceo_supabase):
        ceo_supabase.tables["vt_founder_business_insights"] = [
            {"id": "i1", "title": "X", "category": "umsatzrisiko", "status": "archiviert"},
        ]
        ceo_supabase.tables["vt_automation_alerts"] = []
        ceo_supabase.tables["vt_affiliate_products"] = []
        assert risk_opp.list_executive_risks() == []

    def test_opportunities_aggregate_from_insights_and_automation(self, ceo_supabase):
        ceo_supabase.tables["vt_founder_business_insights"] = [
            {"id": "i2", "title": "Chance A", "category": "affiliate_chance", "status": "erkannt"},
        ]
        ceo_supabase.tables["vt_automation_opportunities"] = [{"id": "o1", "description": "Wiederholt", "status": "neu", "occurrences": 5}]
        opportunities = risk_opp.list_executive_opportunities()
        refs = {o["ref"] for o in opportunities}
        assert "insight:i2" in refs
        assert "automation_opportunity:o1" in refs

    def test_close_risk_updates_underlying_insight_status(self, ceo_supabase):
        ceo_supabase.tables["vt_founder_business_insights"] = [{"id": "i1", "status": "erkannt"}]
        risk_opp.close_executive_risk("insight:i1", closed_by="founder@example.com")
        assert ceo_supabase.tables["vt_founder_business_insights"][0]["status"] == "archiviert"

    def test_close_risk_rejects_unresolvable_source(self, ceo_supabase):
        with pytest.raises(ValueError):
            risk_opp.close_executive_risk("affiliate_product:p1", closed_by="x")

    def test_send_to_task_is_idempotent(self, ceo_supabase):
        first = risk_opp.send_to_task_manager("insight:i1", title="X", reason="Y")
        second = risk_opp.send_to_task_manager("insight:i1", title="X", reason="Y")
        assert first == second
        assert len(ceo_supabase.tables["vt_founder_tasks"]) == 1

    def test_send_to_approval_never_auto_executes(self, ceo_supabase):
        approval_id = risk_opp.send_to_approval_center("insight:i1", title="Preisexperiment", reason="Y", category="business")
        row = ceo_supabase.tables["vt_founder_approvals"][0]
        assert row["status"] == "ki_geprueft"  # never auto-'freigegeben'
        assert approval_id is not None

    def test_data_quality_risk_detected_when_many_metrics_degraded(self, ceo_supabase, monkeypatch):
        monkeypatch.setattr(ex_metrics, "get_ceo_overview", lambda: {
            k: {"data_quality": "nicht_verbunden"} for k in risk_opp.CRITICAL_METRIC_KEYS
        })
        risk_opp.detect_data_quality_risk()
        insights = ceo_supabase.tables["vt_founder_business_insights"]
        assert any(i["category"] == "datenqualitaetsrisiko" for i in insights)

    def test_no_data_quality_risk_when_metrics_healthy(self, ceo_supabase, monkeypatch):
        monkeypatch.setattr(ex_metrics, "get_ceo_overview", lambda: {
            k: {"data_quality": "vollstaendig"} for k in risk_opp.CRITICAL_METRIC_KEYS
        })
        risk_opp.detect_data_quality_risk()
        assert ceo_supabase.tables.get("vt_founder_business_insights", []) == []


class TestScenarioPlanning:
    def test_premium_conversion_up_is_computable(self, ceo_supabase):
        ceo_supabase.tables["vt_users"] = [{"premium": True}] * 10 + [{"premium": False}] * 90
        result = executive_scenarios.run_scenario("premium_conversion_up", delta_pct=10)
        assert result["computable"] is True
        assert result["projected"]["premium_users"] >= result["baseline"]["premium_users"]

    def test_churn_down_is_honestly_not_computable(self, ceo_supabase):
        result = executive_scenarios.run_scenario("churn_down", delta_pct=10)
        assert result["computable"] is False
        assert "Kündigung" in result["reason"]

    def test_ai_cost_up_is_honestly_not_computable(self, ceo_supabase):
        result = executive_scenarios.run_scenario("ai_cost_up", delta_pct=10)
        assert result["computable"] is False

    def test_annual_plan_share_up_is_honestly_not_computable(self, ceo_supabase):
        result = executive_scenarios.run_scenario("annual_plan_share_up", delta_pct=10)
        assert result["computable"] is False

    def test_invalid_scenario_type_rejected(self, ceo_supabase):
        with pytest.raises(ValueError):
            executive_scenarios.run_scenario("invalid_type", delta_pct=1)

    def test_affiliate_ctr_up_without_data_not_computable(self, ceo_supabase):
        ceo_supabase.tables["vt_affiliate_events"] = []
        result = executive_scenarios.run_scenario("affiliate_ctr_up", delta_pct=10)
        assert result["computable"] is False

    def test_no_scenario_ever_changes_a_price(self, ceo_supabase):
        ceo_supabase.tables["vt_users"] = [{"premium": True}] * 10 + [{"premium": False}] * 90
        result = executive_scenarios.run_scenario("premium_conversion_up", delta_pct=10)
        assert "new_price" not in result and "price_updated" not in result
        assert result["computable"] is True  # only user-count effect, never an actual price mutation

    def test_save_and_list_scenario(self, ceo_supabase):
        ceo_supabase.tables["vt_users"] = [{"premium": True}] * 10 + [{"premium": False}] * 90
        executive_scenarios.save_scenario(name="Test", scenario_type="premium_conversion_up", delta_pct=5, created_by="f@x.com")
        assert len(executive_scenarios.list_scenarios()) == 1


class TestExecutiveSummary:
    def test_summary_has_all_required_fields(self, ceo_supabase):
        summary = executive_summary.compute_executive_summary("daily")
        for field in ("whats_going_well", "whats_going_badly", "whats_changed", "goals_on_track", "goals_at_risk", "top_risks", "top_opportunities", "open_founder_decisions"):
            assert field in summary

    def test_daily_briefing_snippet_never_raises(self, ceo_supabase, monkeypatch):
        monkeypatch.setattr(executive_summary, "compute_executive_summary", lambda period: (_ for _ in ()).throw(RuntimeError("boom")))
        snippet = executive_summary.get_ceo_daily_briefing_snippet()
        assert snippet["note"] == "CEO Intelligence noch nicht verfügbar."


class TestCeoIntelligenceRouterPermissions:
    @pytest.mark.anyio
    async def test_overview_requires_view_permission(self, ceo_supabase, ceo_permission_spy):
        await ceo_router.ceo_overview(authorization="Bearer x")
        assert ceo_permission_spy[-1] == ("Bearer x", "view_ceo_intelligence")

    @pytest.mark.anyio
    async def test_create_goal_requires_manage_permission(self, ceo_supabase, ceo_permission_spy):
        data = ceo_router.GoalInput(title="1000 aktive Nutzer", category="aktive_nutzer", target_value=1000)
        await ceo_router.create_goal(data, authorization="Bearer x")
        assert ceo_permission_spy[-1] == ("Bearer x", "manage_ceo_intelligence")

    @pytest.mark.anyio
    async def test_export_requires_manage_permission(self, ceo_supabase, ceo_permission_spy):
        result = await ceo_router.export_data(resource="scorecard", format="json", authorization="Bearer x")
        assert ceo_permission_spy[-1] == ("Bearer x", "manage_ceo_intelligence")
        assert result["resource"] == "scorecard"

    @pytest.mark.anyio
    async def test_export_rejects_unknown_resource(self, ceo_supabase, ceo_permission_spy):
        with pytest.raises(HTTPException) as exc_info:
            await ceo_router.export_data(resource="secrets", format="json", authorization="Bearer x")
        assert exc_info.value.status_code == 400

    @pytest.mark.anyio
    async def test_export_csv_format(self, ceo_supabase, ceo_permission_spy):
        result = await ceo_router.export_data(resource="scorecard", format="csv", authorization="Bearer x")
        assert "csv" in result


class TestAskCeoIntelligence:
    @pytest.mark.anyio
    async def test_insufficient_data_returns_honest_message(self, ceo_supabase, ceo_permission_spy):
        data = ceo_router.AskInput(question="Wo verliere ich Umsatz?")
        result = await ceo_router.ask_ceo_intelligence(data, request=SimpleNamespace(client=SimpleNamespace(host="127.0.0.20")), authorization="Bearer x")
        assert result["insufficient_data"] is True
        assert result["answer"] == ceo_router.INSUFFICIENT_DATA_MESSAGE

    @pytest.mark.anyio
    async def test_provider_failure_returns_503_never_fabricates(self, ceo_supabase, ceo_permission_spy, monkeypatch):
        ceo_supabase.tables["vt_users"] = [{"premium": False}] * 10

        class _FailingProvider:
            async def generate_recommendation_explanation(self, *, system_prompt, context_text):
                raise AIProviderUnavailableError("down")

        monkeypatch.setattr(ceo_router, "_get_ai_provider", lambda: _FailingProvider())
        data = ceo_router.AskInput(question="Welche 3 Bereiche brauchen Aufmerksamkeit?")
        with pytest.raises(HTTPException) as exc_info:
            await ceo_router.ask_ceo_intelligence(data, request=SimpleNamespace(client=SimpleNamespace(host="127.0.0.21")), authorization="Bearer x")
        assert exc_info.value.status_code == 503

    @pytest.mark.anyio
    async def test_successful_answer_is_grounded(self, ceo_supabase, ceo_permission_spy, monkeypatch):
        ceo_supabase.tables["vt_users"] = [{"premium": False}] * 10

        class _FakeProvider:
            async def generate_recommendation_explanation(self, *, system_prompt, context_text):
                assert "Frage:" in context_text
                return "Antwort basierend auf echten Daten."

        monkeypatch.setattr(ceo_router, "_get_ai_provider", lambda: _FakeProvider())
        data = ceo_router.AskInput(question="Welche 3 Bereiche brauchen Aufmerksamkeit?")
        result = await ceo_router.ask_ceo_intelligence(data, request=SimpleNamespace(client=SimpleNamespace(host="127.0.0.22")), authorization="Bearer x")
        assert result["insufficient_data"] is False
        assert "echten Daten" in result["answer"]
