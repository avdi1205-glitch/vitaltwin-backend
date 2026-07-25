"""Tests for the Smart Approval Center (VitalTwin Enterprise, Founder
Operating System, Submodule D): `core/founder_approval_detector.py`
(per-item, idempotent proposal detection) and `routers/founder_approval.py`
(list/filter/search, status/comment/priority updates, bulk actions, and
the real entity side-effects for affiliate product/partner approval)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core import founder_approval_detector as detector_module
from app.routers import founder_approval as approval_module


@pytest.fixture
def anyio_backend():
    return "asyncio"


class _FakeApprovalQuery:
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


class _FakeApprovalSupabase:
    def __init__(self, tables: dict[str, list[dict]] | None = None):
        self.tables = tables or {}

    def table(self, name):
        rows = self.tables.setdefault(name, [])
        return _FakeApprovalQuery(rows)


@pytest.fixture
def approval_supabase(monkeypatch):
    fake = _FakeApprovalSupabase()
    monkeypatch.setattr(detector_module, "supabase", fake)
    monkeypatch.setattr(approval_module, "supabase", fake)
    return fake


@pytest.fixture
def approval_permission_spy(monkeypatch):
    calls: list[tuple] = []

    def _fake(authorization, permission):
        calls.append((authorization, permission))
        return SimpleNamespace(email="founder@example.com", role="super_admin")

    monkeypatch.setattr(approval_module, "require_admin_permission", _fake)
    return calls


class TestApprovalDetector:
    def test_creates_one_proposal_per_pending_product(self, approval_supabase):
        approval_supabase.tables["vt_affiliate_products"] = [
            {"id": "p1", "title": "A", "status": "in_review", "link_status": "ok", "created_at": "2000-01-01"},
            {"id": "p2", "title": "B", "status": "in_review", "link_status": "ok", "created_at": "2000-01-01"},
        ]
        detector_module._detect_affiliate_products_pending_approval()
        proposals = approval_supabase.tables["vt_founder_approvals"]
        assert len(proposals) == 2
        assert {p["related_entity_id"] for p in proposals} == {"p1", "p2"}
        assert all(p["status"] == "ki_geprueft" for p in proposals)

    def test_does_not_duplicate_on_rescan(self, approval_supabase):
        approval_supabase.tables["vt_affiliate_products"] = [
            {"id": "p1", "title": "A", "status": "in_review", "link_status": "ok", "created_at": "2000-01-01"},
        ]
        detector_module._detect_affiliate_products_pending_approval()
        detector_module._detect_affiliate_products_pending_approval()
        assert len(approval_supabase.tables["vt_founder_approvals"]) == 1

    def test_does_not_reopen_a_decided_proposal(self, approval_supabase):
        approval_supabase.tables["vt_affiliate_products"] = [
            {"id": "p1", "title": "A", "status": "in_review", "link_status": "ok", "created_at": "2000-01-01"},
        ]
        detector_module._detect_affiliate_products_pending_approval()
        approval_supabase.tables["vt_founder_approvals"][0]["status"] = "abgelehnt"
        detector_module._detect_affiliate_products_pending_approval()
        proposals = approval_supabase.tables["vt_founder_approvals"]
        assert len(proposals) == 1
        assert proposals[0]["status"] == "abgelehnt"

    def test_broken_link_proposal_has_high_priority(self, approval_supabase):
        approval_supabase.tables["vt_affiliate_products"] = [
            {"id": "p1", "title": "A", "affiliate_url": "https://example.com", "link_status": "broken"},
        ]
        detector_module._detect_affiliate_broken_links()
        proposals = approval_supabase.tables["vt_founder_approvals"]
        assert proposals[0]["priority"] == "hoch"
        assert proposals[0]["category"] == "affiliate"

    def test_expired_offer_detected_only_for_still_active_status(self, approval_supabase):
        approval_supabase.tables["vt_affiliate_products"] = [
            {"id": "p1", "title": "Expired", "status": "active", "end_date": "2000-01-01"},
            {"id": "p2", "title": "Already archived", "status": "archived", "end_date": "2000-01-01"},
        ]
        detector_module._detect_expired_offers()
        proposals = approval_supabase.tables["vt_founder_approvals"]
        assert len(proposals) == 1
        assert proposals[0]["related_entity_id"] == "p1"

    def test_new_partner_program_detected(self, approval_supabase):
        approval_supabase.tables["vt_affiliate_partners"] = [
            {"id": "partner1", "network": "awin", "partner_name": "Test Partner", "status": "inactive"},
        ]
        detector_module._detect_new_partner_programs()
        proposals = approval_supabase.tables["vt_founder_approvals"]
        assert proposals[0]["category"] == "business"
        assert proposals[0]["related_entity_type"] == "affiliate_partner"

    def test_support_feedback_high_priority_for_low_score(self, approval_supabase):
        approval_supabase.tables["vt_user_feedback"] = [
            {"id": "f1", "message": "schlecht", "score": 1, "created_at": "2099-01-01T00:00:00+00:00"},
        ]
        detector_module._detect_support_feedback()
        proposals = approval_supabase.tables["vt_founder_approvals"]
        assert proposals[0]["priority"] == "hoch"

    def test_run_detection_covers_all_rules_without_crashing(self, approval_supabase):
        approval_supabase.tables["vt_affiliate_products"] = [
            {"id": "p1", "title": "A", "status": "in_review", "link_status": "broken", "end_date": None, "created_at": "2000-01-01", "affiliate_url": "https://example.com"},
        ]
        approval_supabase.tables["vt_affiliate_partners"] = [{"id": "partner1", "network": "awin", "partner_name": "X", "status": "inactive"}]
        approval_supabase.tables["vt_user_feedback"] = []
        detector_module.run_detection()
        dedupe_keys = {p["dedupe_key"] for p in approval_supabase.tables["vt_founder_approvals"]}
        assert "affiliate_product_pending_p1" in dedupe_keys
        assert "affiliate_link_broken_p1" in dedupe_keys
        assert "affiliate_partner_new_partner1" in dedupe_keys


class TestApprovalRouter:
    @pytest.mark.anyio
    async def test_list_requires_view_permission(self, approval_supabase, approval_permission_spy):
        await approval_module.list_approvals(authorization="Bearer x")
        assert approval_permission_spy[-1] == ("Bearer x", "view_founder_os")

    @pytest.mark.anyio
    async def test_list_filters_by_category(self, approval_supabase, approval_permission_spy):
        approval_supabase.tables["vt_founder_approvals"] = [
            {"id": "a1", "category": "affiliate", "status": "ki_geprueft", "priority": "mittel", "created_at": "2000-01-01"},
            {"id": "a2", "category": "support", "status": "ki_geprueft", "priority": "mittel", "created_at": "2000-01-01"},
        ]
        result = await approval_module.list_approvals(category="support", authorization="Bearer x")
        assert len(result["items"]) == 1
        assert result["items"][0]["id"] == "a2"
        assert result["summary"]["total"] == 2

    @pytest.mark.anyio
    async def test_list_search_matches_title(self, approval_supabase, approval_permission_spy):
        approval_supabase.tables["vt_founder_approvals"] = [
            {"id": "a1", "title": "Defekter Link bei Produkt X", "category": "affiliate", "status": "ki_geprueft", "priority": "hoch", "created_at": "2000-01-01"},
        ]
        result = await approval_module.list_approvals(search="Produkt X", authorization="Bearer x")
        assert len(result["items"]) == 1

    @pytest.mark.anyio
    async def test_update_status_requires_manage_permission(self, approval_supabase, approval_permission_spy):
        approval_supabase.tables["vt_founder_approvals"] = [{"id": "a1", "status": "ki_geprueft"}]
        data = approval_module.StatusInput(status="zur_pruefung")
        await approval_module.update_approval_status("a1", data, authorization="Bearer x")
        assert approval_permission_spy[-1] == ("Bearer x", "manage_founder_os")
        assert approval_supabase.tables["vt_founder_approvals"][0]["status"] == "zur_pruefung"

    @pytest.mark.anyio
    async def test_invalid_status_rejected(self):
        with pytest.raises(ValueError):
            approval_module.StatusInput(status="not_real")

    @pytest.mark.anyio
    async def test_approving_affiliate_product_proposal_updates_real_product(self, approval_supabase, approval_permission_spy):
        approval_supabase.tables["vt_founder_approvals"] = [
            {"id": "a1", "status": "ki_geprueft", "related_entity_type": "affiliate_product", "related_entity_id": "p1"}
        ]
        approval_supabase.tables["vt_affiliate_products"] = [{"id": "p1", "status": "in_review"}]
        data = approval_module.StatusInput(status="freigegeben")
        await approval_module.update_approval_status("a1", data, authorization="Bearer x")
        assert approval_supabase.tables["vt_affiliate_products"][0]["status"] == "approved"

    @pytest.mark.anyio
    async def test_rejecting_affiliate_partner_proposal_keeps_partner_inactive(self, approval_supabase, approval_permission_spy):
        approval_supabase.tables["vt_founder_approvals"] = [
            {"id": "a1", "status": "ki_geprueft", "related_entity_type": "affiliate_partner", "related_entity_id": "partner1"}
        ]
        approval_supabase.tables["vt_affiliate_partners"] = [{"id": "partner1", "status": "inactive"}]
        data = approval_module.StatusInput(status="abgelehnt")
        await approval_module.update_approval_status("a1", data, authorization="Bearer x")
        assert approval_supabase.tables["vt_affiliate_partners"][0]["status"] == "inactive"

    @pytest.mark.anyio
    async def test_support_proposal_has_no_entity_side_effect(self, approval_supabase, approval_permission_spy):
        approval_supabase.tables["vt_founder_approvals"] = [
            {"id": "a1", "status": "ki_geprueft", "related_entity_type": None, "related_entity_id": "f1"}
        ]
        data = approval_module.StatusInput(status="freigegeben")
        result = await approval_module.update_approval_status("a1", data, authorization="Bearer x")
        assert result["message"] == "Status aktualisiert."

    @pytest.mark.anyio
    async def test_comment_endpoint_requires_manage_permission(self, approval_supabase, approval_permission_spy):
        approval_supabase.tables["vt_founder_approvals"] = [{"id": "a1"}]
        data = approval_module.CommentInput(comment="Später ansehen")
        await approval_module.update_approval_comment("a1", data, authorization="Bearer x")
        assert approval_permission_spy[-1] == ("Bearer x", "manage_founder_os")
        assert approval_supabase.tables["vt_founder_approvals"][0]["founder_comment"] == "Später ansehen"

    @pytest.mark.anyio
    async def test_priority_endpoint_validates_value(self):
        with pytest.raises(ValueError):
            approval_module.PriorityInput(priority="super-dringend")

    @pytest.mark.anyio
    async def test_bulk_approve_updates_all_and_applies_side_effects(self, approval_supabase, approval_permission_spy):
        approval_supabase.tables["vt_founder_approvals"] = [
            {"id": "a1", "status": "ki_geprueft", "related_entity_type": "affiliate_product", "related_entity_id": "p1"},
            {"id": "a2", "status": "ki_geprueft", "related_entity_type": "affiliate_product", "related_entity_id": "p2"},
        ]
        approval_supabase.tables["vt_affiliate_products"] = [
            {"id": "p1", "status": "in_review"},
            {"id": "p2", "status": "in_review"},
        ]
        data = approval_module.BulkInput(ids=["a1", "a2"], status="freigegeben")
        result = await approval_module.bulk_update_approvals(data, authorization="Bearer x")
        assert result["updated"] == 2
        assert all(p["status"] == "approved" for p in approval_supabase.tables["vt_affiliate_products"])

    @pytest.mark.anyio
    async def test_bulk_rejects_invalid_status(self):
        with pytest.raises(ValueError):
            approval_module.BulkInput(ids=["a1"], status="zur_pruefung")
