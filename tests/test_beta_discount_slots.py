"""Unit tests for the public `GET /api/beta/discount-slots-remaining`
endpoint and the authenticated `GET /api/beta/my-discount` transparency
endpoint. Mocks the core discount-program functions directly (not
Supabase) — no real network/database access."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.routers import beta as beta_module

client = TestClient(app)


@pytest.fixture(autouse=True)
def no_rate_limit(monkeypatch):
    monkeypatch.setattr(beta_module, "enforce_rate_limit", lambda *a, **k: None)


class TestDiscountSlotsRemaining:
    def test_returns_full_total_when_no_grants_exist_yet(self, monkeypatch):
        monkeypatch.setattr(beta_module, "count_real_claimed_slots", lambda: 0)
        response = client.get("/api/beta/discount-slots-remaining")
        assert response.status_code == 200
        assert response.json() == {"remaining_slots": 20, "total_slots": 20}

    def test_subtracts_real_granted_count(self, monkeypatch):
        monkeypatch.setattr(beta_module, "count_real_claimed_slots", lambda: 7)
        response = client.get("/api/beta/discount-slots-remaining")
        assert response.status_code == 200
        assert response.json() == {"remaining_slots": 13, "total_slots": 20}

    def test_never_goes_negative_once_all_slots_are_taken(self, monkeypatch):
        monkeypatch.setattr(beta_module, "count_real_claimed_slots", lambda: 20)
        response = client.get("/api/beta/discount-slots-remaining")
        assert response.json() == {"remaining_slots": 0, "total_slots": 20}

    def test_falls_back_to_full_total_if_table_does_not_exist_yet(self, monkeypatch):
        def _raise():
            raise Exception('relation "vt_beta_discount_grants" does not exist')

        monkeypatch.setattr(beta_module, "count_real_claimed_slots", _raise)
        response = client.get("/api/beta/discount-slots-remaining")
        assert response.status_code == 200
        assert response.json() == {"remaining_slots": 20, "total_slots": 20}

    def test_total_slots_is_twenty(self):
        assert beta_module.TOTAL_DISCOUNT_SLOTS == 20

    def test_requires_no_authorization_header(self, monkeypatch):
        monkeypatch.setattr(beta_module, "count_real_claimed_slots", lambda: 0)
        response = client.get("/api/beta/discount-slots-remaining")
        assert response.status_code == 200

    def test_response_exposes_no_user_data(self, monkeypatch):
        monkeypatch.setattr(beta_module, "count_real_claimed_slots", lambda: 0)
        response = client.get("/api/beta/discount-slots-remaining")
        assert set(response.json().keys()) == {"remaining_slots", "total_slots"}


class TestMyDiscountGrant:
    def test_requires_authentication(self):
        response = client.get("/api/beta/my-discount")
        assert response.status_code == 401

    def test_returns_no_grant_for_a_user_without_one(self, monkeypatch):
        monkeypatch.setattr(beta_module, "require_email", lambda auth: "user@example.com")
        monkeypatch.setattr(beta_module, "get_discount_grant_for_email", lambda email: None)
        response = client.get("/api/beta/my-discount", headers={"Authorization": "Bearer faketoken"})
        assert response.status_code == 200
        assert response.json() == {"has_grant": False}

    def test_returns_own_grant_details(self, monkeypatch):
        monkeypatch.setattr(beta_module, "require_email", lambda auth: "grantee@example.com")
        monkeypatch.setattr(
            beta_module,
            "get_discount_grant_for_email",
            lambda email: {
                "status": "granted",
                "discount_percent": 50,
                "duration_months": 6,
                "applied_at": None,
            }
            if email == "grantee@example.com"
            else None,
        )
        monkeypatch.setattr(beta_module, "compute_public_rank", lambda email: 3 if email == "grantee@example.com" else None)
        response = client.get("/api/beta/my-discount", headers={"Authorization": "Bearer faketoken"})
        assert response.status_code == 200
        body = response.json()
        assert body["has_grant"] is True
        assert body["rank"] == 3
        assert "slot_number" not in body
        assert body["total_slots"] == 20
        assert body["status"] == "granted"

    def test_never_reveals_another_users_grant(self, monkeypatch):
        monkeypatch.setattr(beta_module, "require_email", lambda auth: "other-user@example.com")
        monkeypatch.setattr(
            beta_module,
            "get_discount_grant_for_email",
            lambda email: {"status": "granted", "discount_percent": 50, "duration_months": 6, "applied_at": None}
            if email == "grantee@example.com"
            else None,
        )
        response = client.get("/api/beta/my-discount", headers={"Authorization": "Bearer faketoken"})
        assert response.json() == {"has_grant": False}

