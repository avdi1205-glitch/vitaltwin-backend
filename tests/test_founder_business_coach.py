"""Tests for the AI Business Coach (VitalTwin Enterprise, Founder
Operating System, Submodule E): `core/founder_business_metrics.py`
(aggregated, privacy-guarded KPIs), `core/founder_business_insight_engine.py`
(rule-based, no-LLM insight detection + Task Manager/Approval Center
handoff), `core/founder_business_goals.py` (honest, non-predictive goal
progress), and `routers/founder_business_coach.py` (permissions, the
grounded AI Q&A endpoint with mocked provider, cost control, automation
score)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core import ai_usage_logger
from app.core import founder_business_goals as goals_module
from app.core import founder_business_insight_engine as insight_engine
from app.core import founder_business_metrics as metrics
from app.core import stripe_billing
from app.routers import founder_business_coach as coach_module
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

    def select(self, *a, count=None, **k):
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


class _FakeSupabase:
    def __init__(self, tables: dict[str, list[dict]] | None = None):
        self.tables = tables or {}

    def table(self, name):
        rows = self.tables.setdefault(name, [])
        return _FakeQuery(rows)


@pytest.fixture
def business_supabase(monkeypatch):
    fake = _FakeSupabase()
    monkeypatch.setattr(metrics, "supabase", fake)
    monkeypatch.setattr(insight_engine, "supabase", fake)
    monkeypatch.setattr(coach_module, "supabase", fake)
    monkeypatch.setattr(stripe_billing, "supabase", fake)
    monkeypatch.setattr(ai_usage_logger, "supabase", fake)
    return fake


@pytest.fixture
def coach_permission_spy(monkeypatch):
    calls: list[tuple] = []

    def _fake(authorization, permission):
        calls.append((authorization, permission))
        return SimpleNamespace(email="founder@example.com", role="super_admin")

    monkeypatch.setattr(coach_module, "require_admin_permission", _fake)
    return calls


class TestSmallGroupGuard:
    def test_suppresses_counts_below_threshold(self):
        value, note = metrics.small_group_guard(3)
        assert value is None
        assert "zu klein" in note

    def test_allows_counts_at_or_above_threshold(self):
        value, note = metrics.small_group_guard(5)
        assert value == 5
        assert note is None

    def test_none_input_is_treated_as_unreachable(self):
        value, note = metrics.small_group_guard(None)
        assert value is None
        assert "nicht erreichbar" in note


class TestBusinessDashboard:
    def test_stripe_revenue_is_real_zero_when_table_reachable_but_empty(self, business_supabase):
        """vt_stripe_payments is reachable (fake) but has zero rows — an
        honest real `0.0`, not `None` (None is reserved for unreachable)."""
        result = metrics.get_business_dashboard()
        assert result["revenue_today"]["value"] == 0.0
        assert result["revenue_today"]["note"] is None
        assert result["mrr"]["value"] is None
        assert result["ai_cost"]["value"] is None
        assert result["infra_cost"]["value"] is None

    def test_conversion_rate_is_real_ratio(self, business_supabase):
        business_supabase.tables["vt_users"] = [
            {"email": "a@example.com", "premium": True},
            {"email": "b@example.com", "premium": False},
        ]
        result = metrics.get_business_dashboard()
        assert result["conversion_rate"]["value"] == 0.5

    def test_affiliate_revenue_is_real_sum(self, business_supabase):
        business_supabase.tables["vt_affiliate_events"] = [
            {"event_type": "conversion", "revenue": 10.0, "created_at": "2099-01-01T00:00:00+00:00"},
            {"event_type": "conversion", "revenue": 5.0, "created_at": "2099-01-01T00:00:00+00:00"},
        ]
        result = metrics.get_business_dashboard()
        assert result["affiliate_revenue_today"]["value"] == pytest.approx(15.0)


class TestInsightDetection:
    def test_no_insight_without_significant_change(self, business_supabase):
        # Flat registrations week over week — no insight should appear.
        business_supabase.tables["vt_users"] = [
            {"email": f"u{i}@example.com", "created_at": "2099-01-01T00:00:00+00:00"} for i in range(6)
        ]
        insight_engine._detect_user_growth()
        assert business_supabase.tables.get("vt_founder_business_insights", []) == []

    def test_no_insight_below_min_group_size(self, business_supabase, monkeypatch):
        # Even with a huge % change, too few absolute users -> suppressed.
        monkeypatch.setattr(insight_engine.metrics, "get_weekly_new_users", lambda: (2, 0))
        insight_engine._detect_user_growth()
        assert business_supabase.tables.get("vt_founder_business_insights", []) == []

    def test_creates_growth_insight_for_significant_real_increase(self, business_supabase, monkeypatch):
        monkeypatch.setattr(insight_engine.metrics, "get_weekly_new_users", lambda: (20, 10))
        insight_engine._detect_user_growth()
        insights = business_supabase.tables["vt_founder_business_insights"]
        assert len(insights) == 1
        assert insights[0]["category"] == "wachstumschance"
        assert insights[0]["status"] == "erkannt"
        assert insights[0]["source"] == "regelbasiert"

    def test_does_not_duplicate_on_rescan(self, business_supabase, monkeypatch):
        monkeypatch.setattr(insight_engine.metrics, "get_weekly_new_users", lambda: (20, 10))
        insight_engine._detect_user_growth()
        insight_engine._detect_user_growth()
        assert len(business_supabase.tables["vt_founder_business_insights"]) == 1

    def test_does_not_reopen_decided_insight(self, business_supabase, monkeypatch):
        monkeypatch.setattr(insight_engine.metrics, "get_weekly_new_users", lambda: (20, 10))
        insight_engine._detect_user_growth()
        business_supabase.tables["vt_founder_business_insights"][0]["status"] = "verworfen"
        monkeypatch.setattr(insight_engine.metrics, "get_weekly_new_users", lambda: (40, 10))
        insight_engine._detect_user_growth()
        insights = business_supabase.tables["vt_founder_business_insights"]
        assert len(insights) == 1
        assert insights[0]["status"] == "verworfen"

    def test_support_volume_insight_severity_scales_with_change(self, business_supabase, monkeypatch):
        monkeypatch.setattr(insight_engine.metrics, "get_weekly_feedback_counts", lambda: (15, 5))
        insight_engine._detect_support_volume_change()
        insights = business_supabase.tables["vt_founder_business_insights"]
        assert insights[0]["category"] == "supportproblem"
        assert insights[0]["severity"] == "hoch"


class TestTaskAndApprovalHandoff:
    def test_send_insight_to_task_manager_creates_task_once(self, business_supabase):
        insight = {"id": "i1", "title": "Test Insight", "severity": "hoch", "category": "wachstumschance", "data_basis": "x", "possible_impact": "y"}
        result = insight_engine.send_insight_to_task_manager(insight, admin_email="founder@example.com")
        assert result is not None
        assert len(business_supabase.tables["vt_founder_tasks"]) == 1
        # Second call must not duplicate:
        result2 = insight_engine.send_insight_to_task_manager(insight, admin_email="founder@example.com")
        assert result2 is None
        assert len(business_supabase.tables["vt_founder_tasks"]) == 1

    def test_send_recommendation_to_approval_center_creates_approval_once(self, business_supabase):
        recommendation = {"id": "r1", "title": "Preisexperiment", "reasoning": "x", "data_basis": "y", "priority": "hoch"}
        result = insight_engine.send_recommendation_to_approval_center(recommendation, admin_email="founder@example.com")
        assert result is not None
        assert len(business_supabase.tables["vt_founder_approvals"]) == 1
        result2 = insight_engine.send_recommendation_to_approval_center(recommendation, admin_email="founder@example.com")
        assert result2 is None
        assert len(business_supabase.tables["vt_founder_approvals"]) == 1


class TestGoalProgress:
    def test_premium_abos_goal_uses_real_count(self, business_supabase, monkeypatch):
        monkeypatch.setattr(goals_module.metrics, "count_rows", lambda table, filters=None, gte=None: 7 if filters else 10)
        value, note = goals_module.compute_goal_progress({"category": "premium_abos"})
        assert value == 7
        assert note == "Automatisch berechnet."

    def test_unsupported_category_is_honest(self):
        value, note = goals_module.compute_goal_progress({"category": "kuendigungsrate"})
        assert value is None
        assert "nicht automatisch berechenbar" in note

    def test_explain_progress_never_guarantees(self):
        goal = {"start_value": 0, "target_value": 100, "start_date": "2000-01-01", "target_date": "2099-01-01"}
        explanation = goals_module.explain_goal_progress(goal, current_value=50)
        assert "next_action" in explanation
        assert explanation["on_track"] in (True, False, None)

    def test_explain_progress_honest_when_uncomputable(self):
        explanation = goals_module.explain_goal_progress({"target_value": 100}, current_value=None)
        assert explanation["on_track"] is None
        assert explanation["at_risk"] is None


class TestBusinessCoachRouterPermissions:
    @pytest.mark.anyio
    async def test_dashboard_requires_view_permission(self, business_supabase, coach_permission_spy):
        await coach_module.business_coach_dashboard(authorization="Bearer x")
        assert coach_permission_spy[-1] == ("Bearer x", "view_founder_os")

    @pytest.mark.anyio
    async def test_create_goal_requires_manage_permission(self, business_supabase, coach_permission_spy):
        data = coach_module.GoalInput(title="Mehr Premium", category="premium_abos", target_value=100)
        await coach_module.create_goal(data, authorization="Bearer x")
        assert coach_permission_spy[-1] == ("Bearer x", "manage_founder_os")

    @pytest.mark.anyio
    async def test_invalid_goal_category_rejected(self):
        with pytest.raises(ValueError):
            coach_module.GoalInput(title="X", category="not_real", target_value=1)

    @pytest.mark.anyio
    async def test_invalid_insight_status_rejected(self):
        with pytest.raises(ValueError):
            coach_module.InsightStatusInput(status="not_real")

    @pytest.mark.anyio
    async def test_send_insight_to_tasks_requires_manage_permission(self, business_supabase, coach_permission_spy):
        business_supabase.tables["vt_founder_business_insights"] = [
            {"id": "i1", "title": "T", "severity": "mittel", "category": "wachstumschance", "data_basis": "x", "possible_impact": "y"}
        ]
        await coach_module.send_insight_to_tasks("i1", authorization="Bearer x")
        assert coach_permission_spy[-1] == ("Bearer x", "manage_founder_os")
        assert len(business_supabase.tables["vt_founder_tasks"]) == 1


class TestAskBusinessCoach:
    @pytest.mark.anyio
    async def test_insufficient_data_when_too_few_users(self, business_supabase, coach_permission_spy):
        business_supabase.tables["vt_users"] = [{"email": "a@example.com", "premium": False}]
        data = coach_module.AskInput(question="Wie entwickelt sich mein Umsatz?")
        result = await coach_module.ask_business_coach(
            data, request=SimpleNamespace(client=SimpleNamespace(host="127.0.0.1")), authorization="Bearer x"
        )
        assert result["insufficient_data"] is True
        assert result["answer"] == coach_module.INSUFFICIENT_DATA_MESSAGE
        # Never fabricated an AI answer:
        assert business_supabase.tables["vt_founder_coach_queries"][0]["answer"] is None

    @pytest.mark.anyio
    async def test_successful_answer_is_grounded_and_recorded(self, business_supabase, coach_permission_spy, monkeypatch):
        business_supabase.tables["vt_users"] = [{"email": f"u{i}@example.com", "premium": i % 2 == 0} for i in range(10)]

        class _FakeProvider:
            async def generate_recommendation_explanation(self, *, system_prompt, context_text):
                assert "Frage:" in context_text
                return "Der Affiliate-Umsatz ist stabil."

        monkeypatch.setattr(coach_module, "_get_ai_provider", lambda: _FakeProvider())
        data = coach_module.AskInput(question="Wie läuft Affiliate?")
        result = await coach_module.ask_business_coach(
            data, request=SimpleNamespace(client=SimpleNamespace(host="127.0.0.1")), authorization="Bearer x"
        )
        assert result["insufficient_data"] is False
        assert result["answer"] == "Der Affiliate-Umsatz ist stabil."
        assert business_supabase.tables["vt_founder_coach_queries"][0]["error"] is None

    @pytest.mark.anyio
    async def test_provider_failure_never_fabricates_answer(self, business_supabase, coach_permission_spy, monkeypatch):
        business_supabase.tables["vt_users"] = [{"email": f"u{i}@example.com", "premium": False} for i in range(10)]

        class _FailingProvider:
            async def generate_recommendation_explanation(self, *, system_prompt, context_text):
                raise AIProviderUnavailableError("Coach gerade nicht erreichbar.")

        monkeypatch.setattr(coach_module, "_get_ai_provider", lambda: _FailingProvider())
        data = coach_module.AskInput(question="Wie läuft mein Business?")
        with pytest.raises(HTTPException) as exc_info:
            await coach_module.ask_business_coach(
                data, request=SimpleNamespace(client=SimpleNamespace(host="127.0.0.2")), authorization="Bearer x"
            )
        assert exc_info.value.status_code == 503
        logged = business_supabase.tables["vt_founder_coach_queries"][0]
        assert logged["answer"] is None
        assert logged["error"] is not None

    @pytest.mark.anyio
    async def test_empty_question_rejected(self, business_supabase, coach_permission_spy):
        business_supabase.tables["vt_users"] = [{"email": f"u{i}@example.com", "premium": False} for i in range(10)]
        data = coach_module.AskInput(question="   ")
        with pytest.raises(HTTPException) as exc_info:
            await coach_module.ask_business_coach(
                data, request=SimpleNamespace(client=SimpleNamespace(host="127.0.0.3")), authorization="Bearer x"
            )
        assert exc_info.value.status_code == 400


class TestCostControlAndAutomationScore:
    @pytest.mark.anyio
    async def test_cost_control_reports_no_token_data_honestly(self, business_supabase, coach_permission_spy):
        result = await coach_module.cost_control_stats(authorization="Bearer x")
        assert result["token_usage"] is None
        assert "AIProvider" in result["token_usage_note"]
        assert result["estimated_cost"] is None

    @pytest.mark.anyio
    async def test_cost_control_computes_real_error_rate(self, business_supabase, coach_permission_spy):
        business_supabase.tables["vt_founder_coach_queries"] = [
            {"error": None, "latency_ms": 100},
            {"error": "boom", "latency_ms": 200},
        ]
        result = await coach_module.cost_control_stats(authorization="Bearer x")
        assert result["total_queries"] == 2
        assert result["error_count"] == 1
        assert result["error_rate"] == 0.5
        assert result["average_latency_ms"] == 150

    @pytest.mark.anyio
    async def test_automation_score_is_computed_not_fixed(self, business_supabase, coach_permission_spy):
        business_supabase.tables["vt_founder_automation_events"] = [
            {"event_type": "insight_erkannt"},
            {"event_type": "aufgabe_erstellt"},
        ]
        business_supabase.tables["vt_founder_tasks"] = [{"status": "neu"}]
        business_supabase.tables["vt_founder_approvals"] = []
        result = await coach_module.automation_score(authorization="Bearer x")
        assert result["auto_detected_insights"] == 1
        assert result["auto_created_tasks"] == 1
        assert result["manual_decisions_required"] == 1
        assert result["automation_percentage"] == 67  # 2 automatic out of 3 total, rounded
