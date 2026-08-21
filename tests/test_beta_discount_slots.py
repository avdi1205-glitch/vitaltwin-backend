"""Unit tests for the public `GET /api/beta/discount-slots-remaining`
endpoint (no auth, no user data, feeds the "first 20 beta testers"
discount counter on the homepage/pricing page). Mocks rate limiting only —
this endpoint touches no database."""

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
    def test_returns_total_slots_when_no_grants_exist_yet(self):
        response = client.get("/api/beta/discount-slots-remaining")
        assert response.status_code == 200
        body = response.json()
        assert body == {
            "remaining_slots": beta_module.TOTAL_DISCOUNT_SLOTS,
            "total_slots": beta_module.TOTAL_DISCOUNT_SLOTS,
        }

    def test_total_slots_is_twenty(self):
        assert beta_module.TOTAL_DISCOUNT_SLOTS == 20

    def test_requires_no_authorization_header(self):
        response = client.get("/api/beta/discount-slots-remaining")
        assert response.status_code == 200

    def test_response_exposes_no_user_data(self):
        response = client.get("/api/beta/discount-slots-remaining")
        body = response.json()
        assert set(body.keys()) == {"remaining_slots", "total_slots"}
