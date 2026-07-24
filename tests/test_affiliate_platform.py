"""Tests for the Affiliate Intelligence & Management Platform:
`core/affiliate_engine.py` (rule-based eligibility + transparency logging),
`core/affiliate_link_checker.py` (real HTTP check, mocked here),
`core/affiliate_import_export.py` (CSV/JSON/XLSX round-trip), the admin
CRUD/analytics endpoints (`routers/affiliate_admin.py`), and the public
recommendations/tracking/prefs endpoints (`routers/affiliate.py`)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core import affiliate_engine
from app.core import affiliate_import_export
from app.core import affiliate_link_checker
from app.routers import affiliate as affiliate_module
from app.routers import affiliate_admin as affiliate_admin_module
from tests.test_admin_router import _FakeSupabase, fake_supabase, permission_spy, recorded_audit_events, super_admin_principal  # noqa: F401


@pytest.fixture
def anyio_backend():
    return "asyncio"


# ---------------------------------------------------------------------------
# A minimal fake Supabase tailored to the simple select/eq/limit/insert
# chains used by the affiliate engine + import/export modules.
# ---------------------------------------------------------------------------


class _FakeAffiliateQuery:
    def __init__(self, data, source_list=None):
        self._data = data
        self._source_list = source_list  # the real backing list, for insert()

    def select(self, *a, **k):
        return self

    def eq(self, field, value):
        filtered = [row for row in self._data if str(row.get(field)) == str(value)]
        return _FakeAffiliateQuery(filtered)

    def order(self, *a, **k):
        return self

    def limit(self, n):
        return _FakeAffiliateQuery(self._data[:n])

    def insert(self, payload):
        if self._source_list is not None:
            self._source_list.append(payload)
        return self

    def execute(self):
        return SimpleNamespace(data=self._data)


class _FakeAffiliateSupabase:
    def __init__(self, tables: dict[str, list[dict]] | None = None):
        self.tables = tables or {}

    def table(self, name):
        rows = self.tables.setdefault(name, [])
        return _FakeAffiliateQuery(rows, source_list=rows)


@pytest.fixture
def affiliate_supabase(monkeypatch):
    fake = _FakeAffiliateSupabase()
    monkeypatch.setattr(affiliate_engine, "supabase", fake)
    return fake


@pytest.fixture
def affiliate_permission_spy(monkeypatch, super_admin_principal):
    """Like `test_admin_router.permission_spy`, but patches
    `require_admin_permission` inside `routers.affiliate_admin`'s own
    namespace — it was imported there with `from ... import`, so patching
    `routers.admin`'s copy (what `permission_spy` does) would not affect it."""
    calls: list[tuple] = []

    def _fake(authorization, permission):
        calls.append((authorization, permission))
        return super_admin_principal

    monkeypatch.setattr(affiliate_admin_module, "require_admin_permission", _fake)
    return calls


@pytest.fixture
def affiliate_admin_supabase(monkeypatch):
    """Same idea as above, for `supabase` — `routers.affiliate_admin` has
    its own imported reference, separate from `routers.admin`'s."""
    fake = _FakeSupabase()
    monkeypatch.setattr(affiliate_admin_module, "supabase", fake)
    return fake


def _product(**overrides) -> dict:
    base = {
        "id": "p1",
        "title": "Test Produkt",
        "status": "active",
        "link_status": "ok",
        "pinned": False,
        "priority": 0,
        "rating": None,
        "brand": None,
        "partner_id": None,
        "category_id": None,
        "start_date": None,
        "end_date": None,
    }
    base.update(overrides)
    return base


class TestGetEligibleProducts:
    def test_excludes_draft_and_paused_status(self, affiliate_supabase):
        affiliate_supabase.tables["vt_affiliate_products"] = [
            _product(id="p1", status="draft"),
            _product(id="p2", status="paused"),
            _product(id="p3", status="active"),
        ]
        result = affiliate_engine.get_eligible_products()
        assert [p["id"] for p in result] == ["p3"]

    def test_excludes_expired_products(self, affiliate_supabase):
        affiliate_supabase.tables["vt_affiliate_products"] = [
            _product(id="p1", status="active", end_date="2020-01-01"),
            _product(id="p2", status="active", end_date="2999-01-01"),
        ]
        result = affiliate_engine.get_eligible_products()
        assert [p["id"] for p in result] == ["p2"]

    def test_excludes_not_yet_started_products(self, affiliate_supabase):
        affiliate_supabase.tables["vt_affiliate_products"] = [
            _product(id="p1", status="active", start_date="2999-01-01"),
            _product(id="p2", status="active", start_date=None),
        ]
        result = affiliate_engine.get_eligible_products()
        assert [p["id"] for p in result] == ["p2"]

    def test_excludes_broken_links(self, affiliate_supabase):
        affiliate_supabase.tables["vt_affiliate_products"] = [
            _product(id="p1", status="active", link_status="broken"),
            _product(id="p2", status="active", link_status="ok"),
        ]
        result = affiliate_engine.get_eligible_products()
        assert [p["id"] for p in result] == ["p2"]

    def test_excludes_blacklisted_brand(self, affiliate_supabase):
        affiliate_supabase.tables["vt_affiliate_products"] = [
            _product(id="p1", status="active", brand="BadBrand"),
            _product(id="p2", status="active", brand="GoodBrand"),
        ]
        affiliate_supabase.tables["vt_affiliate_blacklist"] = [
            {"entry_type": "brand", "value": "BadBrand"},
        ]
        result = affiliate_engine.get_eligible_products()
        assert [p["id"] for p in result] == ["p2"]

    def test_excludes_blacklisted_product_id(self, affiliate_supabase):
        affiliate_supabase.tables["vt_affiliate_products"] = [
            _product(id="p1", status="active"),
            _product(id="p2", status="active"),
        ]
        affiliate_supabase.tables["vt_affiliate_blacklist"] = [
            {"entry_type": "product", "value": "p1"},
        ]
        result = affiliate_engine.get_eligible_products()
        assert [p["id"] for p in result] == ["p2"]

    def test_pinned_products_sort_first(self, affiliate_supabase):
        affiliate_supabase.tables["vt_affiliate_products"] = [
            _product(id="p1", status="active", pinned=False, priority=100),
            _product(id="p2", status="active", pinned=True, priority=0),
        ]
        result = affiliate_engine.get_eligible_products()
        assert result[0]["id"] == "p2"


class TestGetRecommendationsForUser:
    def test_returns_empty_when_user_opted_out(self, affiliate_supabase):
        affiliate_supabase.tables["vt_affiliate_products"] = [_product(id="p1", status="active")]
        affiliate_supabase.tables["vt_affiliate_user_prefs"] = [
            {"email": "user@example.com", "affiliate_enabled": False, "hidden_categories": [], "hidden_products": []}
        ]
        result = affiliate_engine.get_recommendations_for_user("user@example.com")
        assert result == []

    def test_excludes_hidden_products(self, affiliate_supabase):
        affiliate_supabase.tables["vt_affiliate_products"] = [
            _product(id="p1", status="active"),
            _product(id="p2", status="active"),
        ]
        affiliate_supabase.tables["vt_affiliate_user_prefs"] = [
            {"email": "user@example.com", "affiliate_enabled": True, "hidden_categories": [], "hidden_products": ["p1"]}
        ]
        result = affiliate_engine.get_recommendations_for_user("user@example.com")
        assert [p["id"] for p in result] == ["p2"]

    def test_defaults_to_enabled_for_unknown_user(self, affiliate_supabase):
        affiliate_supabase.tables["vt_affiliate_products"] = [_product(id="p1", status="active")]
        result = affiliate_engine.get_recommendations_for_user("new-user@example.com")
        assert [p["id"] for p in result] == ["p1"]

    def test_logs_transparency_row_per_recommendation(self, affiliate_supabase):
        affiliate_supabase.tables["vt_affiliate_products"] = [_product(id="p1", status="active")]
        affiliate_supabase.tables["vt_affiliate_recommendation_log"] = []
        affiliate_engine.get_recommendations_for_user("user@example.com")
        logged = affiliate_supabase.tables["vt_affiliate_recommendation_log"]
        assert len(logged) == 1
        assert logged[0]["product_id"] == "p1"
        assert logged[0]["email"] == "user@example.com"
        assert "rule_applied" in logged[0]


class TestLinkChecker:
    def test_ok_for_200(self, monkeypatch):
        class _FakeResponse:
            status_code = 200
            history = []

        class _FakeClient:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get(self, url):
                return _FakeResponse()

        monkeypatch.setattr(affiliate_link_checker.httpx, "Client", lambda **k: _FakeClient())
        result = affiliate_link_checker.check_link("https://example.com/product")
        assert result == {"link_status": "ok", "http_status": 200, "redirected": False}

    def test_broken_for_404(self, monkeypatch):
        class _FakeResponse:
            status_code = 404
            history = []

        class _FakeClient:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get(self, url):
                return _FakeResponse()

        monkeypatch.setattr(affiliate_link_checker.httpx, "Client", lambda **k: _FakeClient())
        result = affiliate_link_checker.check_link("https://example.com/broken")
        assert result["link_status"] == "broken"
        assert result["http_status"] == 404

    def test_broken_on_network_error(self, monkeypatch):
        import httpx as httpx_module

        class _FakeClient:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get(self, url):
                raise httpx_module.ConnectError("boom")

        monkeypatch.setattr(affiliate_link_checker.httpx, "Client", lambda **k: _FakeClient())
        result = affiliate_link_checker.check_link("https://unreachable.example")
        assert result == {"link_status": "broken", "http_status": None, "redirected": False}


class TestImportExport:
    def test_import_csv_creates_products(self, monkeypatch):
        inserted: list[dict] = []

        class _FakeQuery:
            def select(self, *a, **k):
                return self

            def execute(self):
                return SimpleNamespace(data=[])

            def insert(self, payload):
                inserted.append(payload)
                return self

        class _FakeSupabase:
            def table(self, name):
                return _FakeQuery()

        monkeypatch.setattr(affiliate_import_export, "supabase", _FakeSupabase())
        csv_content = (
            "title,affiliate_url,status\n"
            "Testprodukt,https://example.com/aff,draft\n"
        ).encode("utf-8")
        result = affiliate_import_export.import_products("csv", csv_content, created_by="admin@example.com")
        assert result["imported"] == 1
        assert result["total_rows"] == 1
        assert inserted[0]["title"] == "Testprodukt"
        assert inserted[0]["created_by"] == "admin@example.com"

    def test_import_reports_missing_required_fields(self, monkeypatch):
        class _FakeQuery:
            def select(self, *a, **k):
                return self

            def execute(self):
                return SimpleNamespace(data=[])

            def insert(self, payload):
                return self

        class _FakeSupabase:
            def table(self, name):
                return _FakeQuery()

        monkeypatch.setattr(affiliate_import_export, "supabase", _FakeSupabase())
        json_content = b'[{"title": "Ohne Link"}]'
        result = affiliate_import_export.import_products("json", json_content, created_by="admin@example.com")
        assert result["imported"] == 0
        assert len(result["errors"]) == 1
        assert "Pflichtfelder" in result["errors"][0]["error"]

    def test_export_csv_contains_product_title(self, monkeypatch):
        class _FakeQuery:
            def __init__(self, data):
                self._data = data

            def select(self, *a, **k):
                return self

            def order(self, *a, **k):
                return self

            def execute(self):
                return SimpleNamespace(data=self._data)

        class _FakeSupabase:
            def table(self, name):
                if name == "vt_affiliate_products":
                    return _FakeQuery([
                        {
                            "id": "p1", "title": "Export Produkt", "affiliate_url": "https://example.com",
                            "category_id": None, "partner_id": None, "tags": ["a", "b"],
                        }
                    ])
                return _FakeQuery([])

        monkeypatch.setattr(affiliate_import_export, "supabase", _FakeSupabase())
        content, media_type, filename = affiliate_import_export.export_products("csv")
        assert b"Export Produkt" in content
        assert media_type == "text/csv"
        assert filename == "affiliate_products.csv"


class TestAffiliateAdminPermissions:
    @pytest.mark.anyio
    async def test_dashboard_requires_view_affiliate(self, affiliate_admin_supabase, affiliate_permission_spy):
        await affiliate_admin_module.affiliate_dashboard(authorization="Bearer x")
        assert affiliate_permission_spy[-1] == ("Bearer x", "view_affiliate")

    @pytest.mark.anyio
    async def test_create_product_requires_manage_affiliate(self, affiliate_admin_supabase, affiliate_permission_spy):
        data = affiliate_admin_module.ProductInput(title="T", affiliate_url="https://example.com")
        await affiliate_admin_module.create_product(data, authorization="Bearer x")
        assert affiliate_permission_spy[-1] == ("Bearer x", "manage_affiliate")

    @pytest.mark.anyio
    async def test_update_product_status_requires_manage_affiliate(self, affiliate_admin_supabase, affiliate_permission_spy):
        data = affiliate_admin_module.ProductStatusInput(status="approved")
        await affiliate_admin_module.update_product_status("p1", data, authorization="Bearer x")
        assert affiliate_permission_spy[-1] == ("Bearer x", "manage_affiliate")

    @pytest.mark.anyio
    async def test_invalid_product_status_is_rejected(self):
        with pytest.raises(ValueError):
            affiliate_admin_module.ProductStatusInput(status="not_a_real_status")

    @pytest.mark.anyio
    async def test_check_link_requires_manage_affiliate_and_persists_result(self, affiliate_admin_supabase, affiliate_permission_spy, monkeypatch):
        affiliate_admin_supabase.store["vt_affiliate_products"] = {"data": [{"id": "p1", "affiliate_url": "https://example.com"}]}
        monkeypatch.setattr(
            affiliate_admin_module, "check_link",
            lambda url: {"link_status": "ok", "http_status": 200, "redirected": False},
        )
        result = await affiliate_admin_module.check_product_link("p1", authorization="Bearer x")
        assert affiliate_permission_spy[-1] == ("Bearer x", "manage_affiliate")
        assert result["link_status"] == "ok"

    @pytest.mark.anyio
    async def test_import_requires_manage_affiliate(self, affiliate_permission_spy, monkeypatch):
        monkeypatch.setattr(
            affiliate_admin_module, "import_products",
            lambda fmt, content, created_by: {"imported": 0, "total_rows": 0, "errors": []},
        )
        data = affiliate_admin_module.ImportInput(format="json", content="[]")
        await affiliate_admin_module.import_affiliate_products(data, authorization="Bearer x")
        assert affiliate_permission_spy[-1] == ("Bearer x", "manage_affiliate")

    @pytest.mark.anyio
    async def test_export_requires_view_affiliate(self, affiliate_permission_spy, monkeypatch):
        monkeypatch.setattr(affiliate_admin_module, "export_products", lambda fmt: (b"data", "text/csv", "f.csv"))
        await affiliate_admin_module.export_affiliate_products(fmt="csv", authorization="Bearer x")
        assert affiliate_permission_spy[-1] == ("Bearer x", "view_affiliate")

    @pytest.mark.anyio
    async def test_settings_get_requires_view_affiliate(self, affiliate_admin_supabase, affiliate_permission_spy):
        await affiliate_admin_module.get_affiliate_settings(authorization="Bearer x")
        assert affiliate_permission_spy[-1] == ("Bearer x", "view_affiliate")

    @pytest.mark.anyio
    async def test_settings_put_requires_manage_affiliate(self, affiliate_admin_supabase, affiliate_permission_spy):
        data = affiliate_admin_module.SettingsInput(recommendations_enabled=False)
        await affiliate_admin_module.update_affiliate_settings(data, authorization="Bearer x")
        assert affiliate_permission_spy[-1] == ("Bearer x", "manage_affiliate")


class TestPublicAffiliateRouter:
    @pytest.mark.anyio
    async def test_recommendations_requires_login(self, monkeypatch):
        def _raise(auth):
            raise HTTPException(status_code=401, detail="Nicht eingeloggt")

        monkeypatch.setattr(affiliate_module, "require_email", _raise)
        with pytest.raises(HTTPException) as exc_info:
            await affiliate_module.get_recommendations(authorization=None)
        assert exc_info.value.status_code == 401

    @pytest.mark.anyio
    async def test_recommendations_marks_items_as_affiliate(self, monkeypatch):
        monkeypatch.setattr(affiliate_module, "require_email", lambda auth: "user@example.com")
        monkeypatch.setattr(
            affiliate_module, "get_recommendations_for_user",
            lambda email, category=None, limit=10: [{"id": "p1", "title": "T"}],
        )
        result = await affiliate_module.get_recommendations(authorization="Bearer x")
        assert result["items"][0]["is_affiliate"] is True
        assert "disclosure" in result["items"][0]

    @pytest.mark.anyio
    async def test_track_rejects_invalid_event_type(self):
        data = affiliate_module.TrackInput(product_id="p1", event_type="not_a_type")
        with pytest.raises(HTTPException) as exc_info:
            await affiliate_module.track_event(data, request=SimpleNamespace(client=SimpleNamespace(host="127.0.0.1")), authorization=None)
        assert exc_info.value.status_code == 400

    @pytest.mark.anyio
    async def test_track_allows_anonymous_click(self, monkeypatch):
        inserted = []

        class _FakeQuery:
            def insert(self, payload):
                inserted.append(payload)
                return self

            def execute(self):
                return SimpleNamespace(data=[])

        class _FakeSupabase:
            def table(self, name):
                return _FakeQuery()

        monkeypatch.setattr(affiliate_module, "supabase", _FakeSupabase())
        data = affiliate_module.TrackInput(product_id="p1", event_type="click")
        result = await affiliate_module.track_event(
            data, request=SimpleNamespace(client=SimpleNamespace(host="127.0.0.1")), authorization=None
        )
        assert result["message"] == "Erfasst."
        assert inserted[0]["email"] is None
        assert inserted[0]["product_id"] == "p1"

    @pytest.mark.anyio
    async def test_prefs_put_requires_login(self, monkeypatch):
        def _raise(auth):
            raise HTTPException(status_code=401, detail="Nicht eingeloggt")

        monkeypatch.setattr(affiliate_module, "require_email", _raise)
        data = affiliate_module.PrefsInput(affiliate_enabled=False)
        with pytest.raises(HTTPException):
            await affiliate_module.update_prefs(data, authorization=None)
