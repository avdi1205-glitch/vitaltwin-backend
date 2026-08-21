"""Targeted test confirming `routers.payments.create_checkout` attaches the
user's "first 20 active beta testers" discount Promotion Code to a new
Stripe Checkout Session when they have an unused grant, and omits it
otherwise. Mocks Stripe and Supabase-backed helpers — no real network
access."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.routers import payments as payments_module


@pytest.fixture(autouse=True)
def stripe_configured(monkeypatch):
    monkeypatch.setattr(payments_module.stripe, "api_key", "sk_test_fake")
    monkeypatch.setattr(payments_module, "get_all_configured_price_ids", lambda: {"price_premium_monthly"})
    monkeypatch.setattr(payments_module, "get_email_by_token", lambda token: "customer@example.com")


def _run(coro):
    return asyncio.run(coro)


class TestCreateCheckoutAppliesBetaDiscount:
    def test_attaches_promotion_code_when_an_unused_grant_exists(self, monkeypatch):
        monkeypatch.setattr(payments_module, "get_unused_promotion_code", lambda email: "promo_abc123")
        captured = {}

        def _fake_create(**kwargs):
            captured.update(kwargs)
            return SimpleNamespace(url="https://checkout.stripe.com/fake")

        monkeypatch.setattr(payments_module.stripe.checkout.Session, "create", _fake_create)

        result = _run(payments_module.create_checkout(payments_module.CreateCheckout(price_id="price_premium_monthly", token="t")))

        assert result["url"] == "https://checkout.stripe.com/fake"
        assert captured["discounts"] == [{"promotion_code": "promo_abc123"}]

    def test_no_discounts_key_when_no_grant_exists(self, monkeypatch):
        monkeypatch.setattr(payments_module, "get_unused_promotion_code", lambda email: None)
        captured = {}

        def _fake_create(**kwargs):
            captured.update(kwargs)
            return SimpleNamespace(url="https://checkout.stripe.com/fake")

        monkeypatch.setattr(payments_module.stripe.checkout.Session, "create", _fake_create)

        _run(payments_module.create_checkout(payments_module.CreateCheckout(price_id="price_premium_monthly", token="t")))

        assert "discounts" not in captured
