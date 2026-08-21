"""Unit tests for `app.routers.payments` webhook event handlers and
`app.core.stripe_billing`. Mocks Supabase and the Stripe SDK — no real
network access. Focuses on: (1) each event type routes to the correct
handler and stores real data, (2) `customer.subscription.deleted`
downgrades `premium` to honestly reflect a real cancellation, (3) revenue/
subscription/refund summaries are computed from real rows, never fabricated."""

from __future__ import annotations

from types import SimpleNamespace
from datetime import datetime, timezone

import pytest

from app.core import stripe_billing
from app.routers import payments as payments_module


class _FakeQuery:
    def __init__(self, store: list):
        self._store = store

    def upsert(self, payload, on_conflict=None):
        payload = dict(payload)
        payload.setdefault("created_at", datetime.now(timezone.utc).isoformat())
        key = on_conflict
        if key and payload.get(key) is not None:
            for i, row in enumerate(self._store):
                if row.get(key) == payload.get(key):
                    self._store[i] = {**row, **payload}
                    return self
        self._store.append(payload)
        return self

    def select(self, *a, **k):
        return self

    def eq(self, field, value):
        return _FilteredQuery([r for r in self._store if r.get(field) == value])

    def gte(self, field, value):
        return _FilteredQuery([r for r in self._store if str(r.get(field, "")) >= value])

    def execute(self):
        return SimpleNamespace(data=list(self._store))


class _FilteredQuery(_FakeQuery):
    def __init__(self, rows):
        super().__init__(rows)


class _FakeSupabase:
    def __init__(self):
        self.tables: dict[str, list] = {}

    def table(self, name):
        return _FakeQuery(self.tables.setdefault(name, []))


@pytest.fixture
def fake_supabase(monkeypatch):
    fake = _FakeSupabase()
    monkeypatch.setattr(stripe_billing, "supabase", fake)
    return fake


@pytest.fixture
def set_premium_spy(monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(payments_module, "set_premium_by_email", lambda email, premium: calls.append((email, premium)))
    return calls


@pytest.fixture
def fake_customer_email(monkeypatch):
    monkeypatch.setattr(payments_module, "_resolve_customer_email", lambda customer_id: "user@example.com" if customer_id else None)


@pytest.fixture
def plan_service_spy(monkeypatch):
    """Spies on the VitalTwin Plan System calls the webhook makes —
    `resolve_plan_from_price_id` defaults to returning None (unknown
    price), `set_plan_by_email` records every call it receives."""
    resolve_calls: list[str | None] = []
    set_plan_calls: list[tuple] = []
    monkeypatch.setattr(payments_module, "resolve_plan_from_price_id", lambda price_id: (resolve_calls.append(price_id), None)[1])
    monkeypatch.setattr(payments_module, "set_plan_by_email", lambda email, plan: set_plan_calls.append((email, plan)))
    return SimpleNamespace(resolve_calls=resolve_calls, set_plan_calls=set_plan_calls)


class TestCheckoutCompleted:
    def test_activates_premium_from_metadata_email(self, set_premium_spy):
        payments_module._handle_checkout_completed({"metadata": {"user_email": "A@Example.com"}})
        assert set_premium_spy == [("a@example.com", True)]

    def test_falls_back_to_customer_email(self, set_premium_spy):
        payments_module._handle_checkout_completed({"customer_email": "b@example.com"})
        assert set_premium_spy == [("b@example.com", True)]


class TestSubscriptionUpsert:
    def test_stores_real_subscription_fields(self, fake_supabase, fake_customer_email, plan_service_spy):
        payments_module._handle_subscription_upsert({
            "id": "sub_123",
            "customer": "cus_1",
            "status": "active",
            "cancel_at_period_end": False,
            "current_period_end": 1700000000,
            "items": {"data": [{"price": {"id": "price_abc"}}]},
        })
        rows = fake_supabase.tables["vt_stripe_subscriptions"]
        assert rows[0]["stripe_subscription_id"] == "sub_123"
        assert rows[0]["status"] == "active"
        assert rows[0]["plan_price_id"] == "price_abc"
        assert rows[0]["email"] == "user@example.com"

    def test_upsert_updates_existing_row_on_conflict(self, fake_supabase, fake_customer_email, plan_service_spy):
        payments_module._handle_subscription_upsert({"id": "sub_1", "customer": "cus_1", "status": "trialing"})
        payments_module._handle_subscription_upsert({"id": "sub_1", "customer": "cus_1", "status": "active"})
        rows = fake_supabase.tables["vt_stripe_subscriptions"]
        assert len(rows) == 1
        assert rows[0]["status"] == "active"

    def test_resolves_and_stores_the_actually_purchased_plan(self, fake_supabase, fake_customer_email, monkeypatch):
        """VitalTwin Plan System: an active subscription whose price_id
        resolves to 'pro' must store plan='pro', not a generic 'premium'."""
        monkeypatch.setattr(payments_module, "resolve_plan_from_price_id", lambda price_id: "pro" if price_id == "price_pro_monthly" else None)
        set_plan_calls: list[tuple] = []
        monkeypatch.setattr(payments_module, "set_plan_by_email", lambda email, plan: set_plan_calls.append((email, plan)))

        payments_module._handle_subscription_upsert({
            "id": "sub_pro",
            "customer": "cus_1",
            "status": "active",
            "items": {"data": [{"price": {"id": "price_pro_monthly"}}]},
        })
        assert set_plan_calls == [("user@example.com", "pro")]

    def test_family_price_id_resolves_to_family_plan(self, fake_supabase, fake_customer_email, monkeypatch):
        monkeypatch.setattr(payments_module, "resolve_plan_from_price_id", lambda price_id: "family" if price_id == "price_family_yearly" else None)
        set_plan_calls: list[tuple] = []
        monkeypatch.setattr(payments_module, "set_plan_by_email", lambda email, plan: set_plan_calls.append((email, plan)))

        payments_module._handle_subscription_upsert({
            "id": "sub_family",
            "customer": "cus_1",
            "status": "active",
            "items": {"data": [{"price": {"id": "price_family_yearly"}}]},
        })
        assert set_plan_calls == [("user@example.com", "family")]

    def test_unknown_price_id_falls_back_to_generic_premium(self, fake_supabase, fake_customer_email, plan_service_spy, set_premium_spy):
        """A price_id that doesn't match any currently configured plan (e.g.
        an old/removed price) must still grant access via the legacy
        boolean fallback rather than silently granting nothing."""
        payments_module._handle_subscription_upsert({
            "id": "sub_legacy",
            "customer": "cus_1",
            "status": "active",
            "items": {"data": [{"price": {"id": "price_unknown"}}]},
        })
        assert plan_service_spy.set_plan_calls == []
        assert set_premium_spy == [("user@example.com", True)]

    def test_past_due_status_does_not_touch_the_stored_plan(self, fake_supabase, fake_customer_email, monkeypatch):
        """A status transition to past_due/unpaid is a dunning/retry state,
        not a definitive end — only `customer.subscription.deleted` (or an
        explicit cancellation) should downgrade the plan."""
        resolve_calls: list[str | None] = []
        set_plan_calls: list[tuple] = []
        monkeypatch.setattr(payments_module, "resolve_plan_from_price_id", lambda price_id: (resolve_calls.append(price_id), "pro")[1])
        monkeypatch.setattr(payments_module, "set_plan_by_email", lambda email, plan: set_plan_calls.append((email, plan)))

        payments_module._handle_subscription_upsert({
            "id": "sub_1",
            "customer": "cus_1",
            "status": "past_due",
            "items": {"data": [{"price": {"id": "price_pro_monthly"}}]},
        })
        assert set_plan_calls == []


class TestSubscriptionUpsertMarksBetaDiscountApplied:
    def test_active_subscription_marks_the_discount_grant_applied(self, fake_supabase, fake_customer_email, plan_service_spy, monkeypatch):
        calls: list[str] = []
        monkeypatch.setattr(payments_module, "mark_grant_applied", lambda email: calls.append(email))
        payments_module._handle_subscription_upsert({"id": "sub_1", "customer": "cus_1", "status": "active"})
        assert calls == ["user@example.com"]

    def test_trialing_subscription_also_marks_it_applied(self, fake_supabase, fake_customer_email, plan_service_spy, monkeypatch):
        calls: list[str] = []
        monkeypatch.setattr(payments_module, "mark_grant_applied", lambda email: calls.append(email))
        payments_module._handle_subscription_upsert({"id": "sub_1", "customer": "cus_1", "status": "trialing"})
        assert calls == ["user@example.com"]

    def test_past_due_status_never_marks_it_applied(self, fake_supabase, fake_customer_email, monkeypatch):
        calls: list[str] = []
        monkeypatch.setattr(payments_module, "mark_grant_applied", lambda email: calls.append(email))
        payments_module._handle_subscription_upsert({"id": "sub_1", "customer": "cus_1", "status": "past_due"})
        assert calls == []


class TestSubscriptionDeleted:
    def test_marks_canceled_and_downgrades_to_free_plan(self, fake_supabase, fake_customer_email, monkeypatch):
        set_plan_calls: list[tuple] = []
        monkeypatch.setattr(payments_module, "set_plan_by_email", lambda email, plan: set_plan_calls.append((email, plan)))

        payments_module._handle_subscription_deleted({"id": "sub_1", "customer": "cus_1"})
        rows = fake_supabase.tables["vt_stripe_subscriptions"]
        assert rows[0]["status"] == "canceled"
        assert rows[0]["canceled_at"] is not None
        assert set_plan_calls == [("user@example.com", "free")]

    def test_no_downgrade_if_email_cannot_be_resolved(self, fake_supabase, monkeypatch):
        monkeypatch.setattr(payments_module, "_resolve_customer_email", lambda customer_id: None)
        set_plan_calls: list[tuple] = []
        monkeypatch.setattr(payments_module, "set_plan_by_email", lambda email, plan: set_plan_calls.append((email, plan)))
        payments_module._handle_subscription_deleted({"id": "sub_1", "customer": "cus_1"})
        assert set_plan_calls == []


class TestInvoicePaid:
    def test_records_real_payment_amount(self, fake_supabase):
        payments_module._handle_invoice_paid({
            "id": "in_1", "amount_paid": 2999, "currency": "eur",
            "customer_email": "c@example.com", "customer": "cus_1",
            "status_transitions": {"paid_at": 1700000000},
        })
        rows = fake_supabase.tables["vt_stripe_payments"]
        assert rows[0]["amount_paid"] == 2999
        assert rows[0]["email"] == "c@example.com"


class TestChargeRefunded:
    def test_records_each_refund(self, fake_supabase):
        payments_module._handle_charge_refunded({
            "id": "ch_1", "customer": "cus_1",
            "billing_details": {"email": "d@example.com"},
            "refunds": {"data": [{"id": "re_1", "amount": 500, "currency": "eur", "reason": "requested_by_customer"}]},
        })
        rows = fake_supabase.tables["vt_stripe_refunds"]
        assert rows[0]["stripe_refund_id"] == "re_1"
        assert rows[0]["amount"] == 500
        assert rows[0]["email"] == "d@example.com"


class TestWebhookRouting:
    @pytest.mark.anyio
    async def test_unknown_secret_returns_400(self, monkeypatch):
        monkeypatch.delenv("STRIPE_WEBHOOK_SECRET", raising=False)
        request = SimpleNamespace(body=lambda: _async_bytes(b"{}"))
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            await payments_module.stripe_webhook(request, stripe_signature="sig")
        assert exc_info.value.status_code == 400

    @pytest.mark.anyio
    async def test_dispatches_to_correct_handler(self, monkeypatch):
        monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test")
        called = []
        monkeypatch.setattr(
            payments_module.stripe.Webhook, "construct_event",
            lambda payload, sig, secret: {"type": "invoice.paid", "data": {"object": {"id": "in_1"}}},
        )
        monkeypatch.setitem(payments_module._EVENT_HANDLERS, "invoice.paid", lambda obj: called.append(obj))
        request = SimpleNamespace(body=lambda: _async_bytes(b"{}"))
        result = await payments_module.stripe_webhook(request, stripe_signature="sig")
        assert result == {"received": True}
        assert called == [{"id": "in_1"}]

    @pytest.mark.anyio
    async def test_unhandled_event_type_is_ignored_gracefully(self, monkeypatch):
        monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test")
        monkeypatch.setattr(
            payments_module.stripe.Webhook, "construct_event",
            lambda payload, sig, secret: {"type": "some.other.event", "data": {"object": {}}},
        )
        request = SimpleNamespace(body=lambda: _async_bytes(b"{}"))
        result = await payments_module.stripe_webhook(request, stripe_signature="sig")
        assert result == {"received": True}


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def _async_bytes(value: bytes) -> bytes:
    return value


class TestRevenueSummary:
    def test_sums_real_payments_in_window(self, fake_supabase):
        stripe_billing.record_payment(stripe_invoice_id="in_1", amount_paid=1000)
        stripe_billing.record_payment(stripe_invoice_id="in_2", amount_paid=500)
        summary = stripe_billing.get_revenue_summary()
        assert summary["revenue_today"] == pytest.approx(15.0)
        assert summary["revenue_month"] == pytest.approx(15.0)

    def test_honest_none_when_unreachable(self, monkeypatch):
        class _Broken:
            def table(self, name):
                raise RuntimeError("boom")

        monkeypatch.setattr(stripe_billing, "supabase", _Broken())
        summary = stripe_billing.get_revenue_summary()
        assert summary["revenue_today"] is None
        assert summary["note"]


class TestSubscriptionSummary:
    def test_counts_active_and_canceled(self, fake_supabase):
        stripe_billing.upsert_subscription(email="a@example.com", stripe_subscription_id="s1", status="active")
        stripe_billing.upsert_subscription(email="b@example.com", stripe_subscription_id="s2", status="canceled")
        summary = stripe_billing.get_subscription_summary()
        assert summary["active"] == 1
        assert summary["canceled"] == 1


class TestRefundSummary:
    def test_sums_real_refunds(self, fake_supabase):
        stripe_billing.record_refund(stripe_refund_id="re_1", amount=500)
        stripe_billing.record_refund(stripe_refund_id="re_2", amount=250)
        summary = stripe_billing.get_refund_summary(days=30)
        assert summary["count"] == 2
        assert summary["total"] == pytest.approx(7.5)
