"""Tests for Affiliate Intelligence (VitalTwin Enterprise, Founder
Operating System, Submodule F): provider status honesty
(`core/affiliate_provider.py`), duplicate detection
(`core/affiliate_dedup.py`), product health
(`core/affiliate_product_health.py`), rule-based product review
(`core/affiliate_review_rules.py`), Smart Ranking
(`core/affiliate_ranking.py`), the new Task Manager detectors
(`core/affiliate_intelligence_detector.py`), and the API router
(`routers/founder_affiliate_intelligence.py`) — permissions, approval
assistant, simulator, AI review (mocked provider), automation score."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core import affiliate_dedup as dedup
from app.core import affiliate_intelligence_detector as intel_detector
from app.core import affiliate_product_health as health
from app.core import affiliate_provider as provider_module
from app.core import affiliate_ranking as ranking
from app.core import affiliate_review_rules as review_rules
from app.routers import founder_affiliate_intelligence as intel_router
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
def intel_supabase(monkeypatch):
    fake = _FakeSupabase()
    monkeypatch.setattr(dedup, "supabase", fake)
    monkeypatch.setattr(intel_detector, "supabase", fake)
    monkeypatch.setattr(intel_router, "supabase", fake)
    return fake


@pytest.fixture
def intel_permission_spy(monkeypatch):
    calls: list[tuple] = []

    def _fake(authorization, permission):
        calls.append((authorization, permission))
        return SimpleNamespace(email="founder@example.com", role="super_admin")

    monkeypatch.setattr(intel_router, "require_admin_permission", _fake)
    return calls


class TestProviderStatuses:
    def test_no_network_api_is_falsely_configured(self, monkeypatch):
        for var in ["STRIPE_SECRET_KEY"]:  # unrelated — just ensuring no accidental env leakage
            monkeypatch.delenv(var, raising=False)
        statuses = provider_module.get_provider_statuses()
        network_statuses = [s for s in statuses if s.kind == "network_api"]
        assert len(network_statuses) == 6
        assert all(not s.configured and not s.connection_tested for s in network_statuses)

    def test_manual_import_is_genuinely_configured(self):
        statuses = provider_module.get_provider_statuses()
        manual = next(s for s in statuses if s.id == provider_module.MANUAL_IMPORT_ID)
        assert manual.configured is True
        assert manual.connection_tested is True

    def test_manual_import_provider_delegates_to_existing_import_function(self, monkeypatch):
        calls = []
        monkeypatch.setattr(provider_module, "import_products", lambda fmt, content, created_by: calls.append((fmt, created_by)) or {"imported": 1})
        result = provider_module.ManualImportProvider().sync_products(fmt="json", content=b"[]", created_by="founder@example.com")
        assert result == {"imported": 1}
        assert calls == [("json", "founder@example.com")]


class TestDuplicateDetection:
    def test_finds_duplicate_by_identical_affiliate_url(self):
        product = {"id": "p1", "title": "New", "affiliate_url": "https://example.com/x", "brand": None}
        existing = [{"id": "p2", "title": "Other", "affiliate_url": "https://example.com/x", "brand": None}]
        matches = dedup.find_duplicate_candidates(product, existing_products=existing)
        assert len(matches) == 1
        assert matches[0]["product_id"] == "p2"

    def test_finds_duplicate_by_title_and_brand(self):
        product = {"id": "p1", "title": "Omega 3 Kapseln", "affiliate_url": "https://a.com", "brand": "BrandX"}
        existing = [{"id": "p2", "title": "omega-3 kapseln!!", "affiliate_url": "https://b.com", "brand": "BrandX"}]
        matches = dedup.find_duplicate_candidates(product, existing_products=existing)
        assert len(matches) == 1

    def test_no_match_for_unrelated_products(self):
        product = {"id": "p1", "title": "Schlafmaske", "affiliate_url": "https://a.com", "brand": "BrandX"}
        existing = [{"id": "p2", "title": "Yoga-Matte", "affiliate_url": "https://b.com", "brand": "BrandY"}]
        assert dedup.find_duplicate_candidates(product, existing_products=existing) == []

    def test_create_duplicate_candidates_is_idempotent(self, intel_supabase):
        created_1 = dedup.create_duplicate_candidates("p1", [{"product_id": "p2", "reason": "x"}])
        created_2 = dedup.create_duplicate_candidates("p1", [{"product_id": "p2", "reason": "x"}])
        assert created_1 == 1
        assert created_2 == 0
        assert len(intel_supabase.tables["vt_affiliate_duplicate_candidates"]) == 1


class TestProductHealth:
    def test_healthy_product_has_no_reasons_beyond_confirmation(self):
        product = {"status": "active", "link_status": "ok", "image_url": "x", "description": "x", "category_id": "c1", "region": "DE"}
        result = health.compute_product_health(product, blacklisted=False)
        assert result["status"] == "healthy"

    def test_broken_link_is_critical(self):
        product = {"status": "active", "link_status": "broken", "image_url": "x", "description": "x", "category_id": "c1", "region": "DE"}
        result = health.compute_product_health(product, blacklisted=False)
        assert result["status"] == "critical"
        assert any("defekt" in r for r in result["reasons"])

    def test_paused_product_short_circuits(self):
        result = health.compute_product_health({"status": "paused"}, blacklisted=False)
        assert result["status"] == "paused"

    def test_missing_image_is_a_warning_not_critical(self):
        product = {"status": "active", "link_status": "ok", "image_url": None, "description": "x", "category_id": "c1", "region": "DE"}
        result = health.compute_product_health(product, blacklisted=False)
        assert result["status"] == "warning"


class TestReviewRules:
    def test_blacklisted_product_is_auto_rejected(self):
        result = review_rules.review_product_rule_based(
            {"title": "X", "affiliate_url": "https://a.com", "brand": "B", "description": "d", "image_url": "i", "category_id": "c"},
            category_name="Schlaf", blacklisted=True, has_duplicate_candidate=False,
        )
        assert result["bucket"] == "automatisch_abgelehnt"

    def test_broken_link_bucket(self):
        result = review_rules.review_product_rule_based(
            {"link_status": "broken"}, category_name=None, blacklisted=False, has_duplicate_candidate=False,
        )
        assert result["bucket"] == "link_defekt"

    def test_incomplete_data_bucket(self):
        result = review_rules.review_product_rule_based(
            {"title": "X"}, category_name=None, blacklisted=False, has_duplicate_candidate=False,
        )
        assert result["bucket"] == "daten_unvollstaendig"

    def test_health_claim_keyword_routes_to_regelverstoss(self):
        product = {"title": "X", "affiliate_url": "a", "brand": "b", "description": "Dieses Produkt heilt Schlafstörungen garantiert.", "image_url": "i", "category_id": "c"}
        result = review_rules.review_product_rule_based(product, category_name="Sonstiges", blacklisted=False, has_duplicate_candidate=False)
        assert result["bucket"] == "moeglicher_regelverstoss"

    def test_sensitive_category_without_keywords_routes_to_einzelpruefung(self):
        product = {"title": "X", "affiliate_url": "a", "brand": "b", "description": "Ein normales Produkt.", "image_url": "i", "category_id": "c"}
        result = review_rules.review_product_rule_based(product, category_name="Nahrungsergänzung", blacklisted=False, has_duplicate_candidate=False)
        assert result["bucket"] == "einzelpruefung"

    def test_clean_product_goes_to_sammelfreigabe(self):
        product = {"title": "X", "affiliate_url": "a", "brand": "b", "description": "Eine gute Schlafmaske.", "image_url": "i", "category_id": "c"}
        result = review_rules.review_product_rule_based(product, category_name="Schlaf", blacklisted=False, has_duplicate_candidate=False)
        assert result["bucket"] == "sammelfreigabe"

    def test_summary_mentions_real_counts(self):
        reviews = [{"bucket": "sammelfreigabe"}, {"bucket": "sammelfreigabe"}, {"bucket": "link_defekt"}]
        summary = review_rules.summarize_approval_assistant(reviews)
        assert "3 neue Produkte" in summary
        assert "2 erfüllen" in summary
        assert "1 Link(s)" in summary

    def test_empty_summary_is_honest(self):
        assert review_rules.summarize_approval_assistant([]) == "Keine neuen Produkte zur Prüfung."


class TestRanking:
    def test_quality_over_commission_weighting(self):
        assert ranking.RANKING_WEIGHTS["quality"] > ranking.RANKING_WEIGHTS["commission"]

    def test_complete_product_scores_higher_than_incomplete(self):
        complete = {"title": "X", "affiliate_url": "a", "brand": "b", "description": "d", "image_url": "i", "category_id": "c", "link_status": "ok"}
        incomplete = {"title": "X"}
        score_complete = ranking.compute_product_score(complete)
        score_incomplete = ranking.compute_product_score(incomplete)
        assert score_complete["score"] > score_incomplete["score"]

    def test_relevance_matches_context_category(self):
        product = {"category_id": "cat1", "link_status": "ok"}
        matching = ranking.compute_product_score(product, context_category_id="cat1")
        not_matching = ranking.compute_product_score(product, context_category_id="cat2")
        assert matching["breakdown"]["relevance"] == 1.0
        assert not_matching["breakdown"]["relevance"] == 0.0

    def test_score_is_explainable(self):
        result = ranking.compute_product_score({"link_status": "ok"})
        assert "explanation" in result
        assert len(result["explanation"]) == len(ranking.RANKING_WEIGHTS)


class TestAffiliateIntelligenceTaskDetectors:
    def test_missing_data_creates_task(self, intel_supabase):
        intel_supabase.tables["vt_affiliate_products"] = [
            {"id": "p1", "status": "in_review", "title": "X"},  # missing most fields
        ]
        intel_detector._detect_missing_product_data()
        tasks = intel_supabase.tables["vt_founder_tasks"]
        assert len(tasks) == 1
        assert tasks[0]["dedupe_key"] == "affiliate_intelligence_missing_data"

    def test_no_task_when_all_products_complete(self, intel_supabase):
        intel_supabase.tables["vt_affiliate_products"] = [
            {"id": "p1", "status": "active", "title": "X", "affiliate_url": "a", "brand": "b", "description": "d", "image_url": "i", "category_id": "c"},
        ]
        intel_detector._detect_missing_product_data()
        assert intel_supabase.tables.get("vt_founder_tasks", []) == []

    def test_possible_duplicates_task_reflects_open_candidates(self, intel_supabase):
        intel_supabase.tables["vt_affiliate_duplicate_candidates"] = [{"id": "d1", "status": "moegliches_duplikat"}]
        intel_detector._detect_possible_duplicates()
        tasks = intel_supabase.tables["vt_founder_tasks"]
        assert len(tasks) == 1
        assert "1" in tasks[0]["title"]

    def test_no_duplicate_task_creation_on_rescan(self, intel_supabase):
        intel_supabase.tables["vt_affiliate_duplicate_candidates"] = [{"id": "d1", "status": "moegliches_duplikat"}]
        intel_detector._detect_possible_duplicates()
        intel_detector._detect_possible_duplicates()
        assert len(intel_supabase.tables["vt_founder_tasks"]) == 1


class TestAffiliateIntelligenceRouterPermissions:
    @pytest.mark.anyio
    async def test_dashboard_requires_view_permission(self, intel_supabase, intel_permission_spy):
        await intel_router.affiliate_intelligence_dashboard(authorization="Bearer x")
        assert intel_permission_spy[-1] == ("Bearer x", "view_founder_os")

    @pytest.mark.anyio
    async def test_dashboard_never_shows_fake_connected_apis(self, intel_supabase, intel_permission_spy):
        result = await intel_router.affiliate_intelligence_dashboard(authorization="Bearer x")
        assert result["connected_apis"]["value"] == 0
        assert result["erroring_apis"]["value"] == 0

    @pytest.mark.anyio
    async def test_resolve_duplicate_requires_manage_permission(self, intel_supabase, intel_permission_spy):
        intel_supabase.tables["vt_affiliate_duplicate_candidates"] = [{"id": "d1", "status": "moegliches_duplikat"}]
        data = intel_router.DuplicateResolutionInput(status="bestaetigtes_duplikat")
        await intel_router.resolve_duplicate("d1", data, authorization="Bearer x")
        assert intel_permission_spy[-1] == ("Bearer x", "manage_founder_os")
        assert intel_supabase.tables["vt_affiliate_duplicate_candidates"][0]["status"] == "bestaetigtes_duplikat"

    @pytest.mark.anyio
    async def test_invalid_duplicate_status_rejected(self):
        with pytest.raises(ValueError):
            intel_router.DuplicateResolutionInput(status="geloescht")

    @pytest.mark.anyio
    async def test_approval_assistant_classifies_products(self, intel_supabase, intel_permission_spy):
        intel_supabase.tables["vt_affiliate_products"] = [
            {"id": "p1", "status": "in_review", "title": "Gute Schlafmaske", "affiliate_url": "a", "brand": "b", "description": "d", "image_url": "i", "category_id": None},
        ]
        result = await intel_router.approval_assistant(authorization="Bearer x")
        assert len(result["items"]) == 1
        assert result["items"][0]["bucket"] == "daten_unvollstaendig"

    @pytest.mark.anyio
    async def test_send_bulk_updates_status_and_runs_approval_detection(self, intel_supabase, intel_permission_spy, monkeypatch):
        intel_supabase.tables["vt_affiliate_products"] = [{"id": "p1", "status": "normalized"}]
        called = []
        monkeypatch.setattr(intel_router.founder_approval_detector, "run_detection", lambda: called.append(True))
        data = intel_router.BulkSendInput(product_ids=["p1"])
        result = await intel_router.send_bulk_to_approval(data, authorization="Bearer x")
        assert result["updated"] == 1
        assert intel_supabase.tables["vt_affiliate_products"][0]["status"] == "in_review"
        assert called == [True]


class TestSimulator:
    @pytest.mark.anyio
    async def test_simulate_uses_neutral_context_not_real_user_data(self, intel_supabase, intel_permission_spy, monkeypatch):
        intel_supabase.tables["vt_affiliate_categories"] = [{"id": "cat-schlaf", "name": "Schlaf"}]
        intel_supabase.tables["vt_affiliate_products"] = [
            {"id": "p1", "title": "Schlafmaske", "status": "active", "link_status": "ok", "category_id": "cat-schlaf", "pinned": False, "priority": 0},
        ]
        monkeypatch.setattr("app.core.affiliate_engine.supabase", intel_supabase)
        data = intel_router.SimulateInput(context="Ich möchte besser schlafen")
        result = await intel_router.simulate_recommendations(data, authorization="Bearer x")
        assert result["matched_category"] == "Schlaf"
        assert any(r["product_id"] == "p1" for r in result["recommended"])
        assert all("disclosure" in r for r in result["recommended"])


class TestAiReview:
    @pytest.mark.anyio
    async def test_ai_review_never_fabricates_on_provider_failure(self, intel_supabase, intel_permission_spy, monkeypatch):
        intel_supabase.tables["vt_affiliate_products"] = [{"id": "p1", "title": "X", "description": "d", "brand": "b"}]

        class _FailingProvider:
            async def generate_recommendation_explanation(self, *, system_prompt, context_text):
                raise AIProviderUnavailableError("Nicht erreichbar.")

        monkeypatch.setattr(intel_router, "_get_ai_provider", lambda: _FailingProvider())
        with pytest.raises(HTTPException) as exc_info:
            await intel_router.ai_review_product("p1", request=SimpleNamespace(client=SimpleNamespace(host="127.0.0.9")), authorization="Bearer x")
        assert exc_info.value.status_code == 503

    @pytest.mark.anyio
    async def test_ai_review_success_marks_product_reviewed(self, intel_supabase, intel_permission_spy, monkeypatch):
        intel_supabase.tables["vt_affiliate_products"] = [{"id": "p1", "title": "X", "description": "d", "brand": "b"}]

        class _FakeProvider:
            async def generate_recommendation_explanation(self, *, system_prompt, context_text):
                return "Passt zur Mission, keine Heilversprechen erkannt."

        monkeypatch.setattr(intel_router, "_get_ai_provider", lambda: _FakeProvider())
        result = await intel_router.ai_review_product("p1", request=SimpleNamespace(client=SimpleNamespace(host="127.0.0.10")), authorization="Bearer x")
        assert "Heilversprechen" in result["explanation"]
        assert intel_supabase.tables["vt_affiliate_products"][0]["ai_reviewed"] is True


class TestAutomationScore:
    @pytest.mark.anyio
    async def test_automation_score_is_computed_from_real_counts(self, intel_supabase, intel_permission_spy):
        intel_supabase.tables["vt_affiliate_products"] = [
            {"status": "active", "link_last_checked_at": "2099-01-01", "ai_reviewed": True},
            {"status": "needs_review", "link_last_checked_at": None, "ai_reviewed": False},
        ]
        intel_supabase.tables["vt_affiliate_duplicate_candidates"] = [{"status": "moegliches_duplikat"}]
        result = await intel_router.automation_score(authorization="Bearer x")
        assert result["auto_checked_links"] == 1
        assert result["auto_detected_duplicates"] == 1
        assert result["manual_decisions_required"] == 2  # needs_review product + open duplicate
        assert result["automation_percentage"] is not None
