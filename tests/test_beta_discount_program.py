"""Unit tests for `core/beta_discount_program.py` — the "first 20 active
beta testers" 50%-off-6-months discount program. Mocks Supabase (table +
rpc) and the `stripe` SDK — no real network/database/Stripe access."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.core import beta_discount_program as program


class _FakeQuery:
    def __init__(self, rows):
        self._rows = list(rows)

    def select(self, *args, **kwargs):
        return self

    def eq(self, field, value):
        self._rows = [r for r in self._rows if r.get(field) == value]
        return self

    def in_(self, field, values):
        value_set = set(values)
        self._rows = [r for r in self._rows if r.get(field) in value_set]
        return self

    def order(self, field, desc=False):
        self._rows = sorted(self._rows, key=lambda r: r.get(field) or "", reverse=desc)
        return self

    def limit(self, n):
        self._rows = self._rows[:n]
        return self

    def update(self, payload):
        self._update_payload = payload
        return self

    def execute(self):
        return SimpleNamespace(data=self._rows, count=len(self._rows))


class _FakeSupabase:
    """Table-aware fake: each named table has its own row list, `.rpc()`
    calls a preconfigured callable instead of touching a real function."""

    def __init__(self, tables: dict[str, list[dict]] | None = None, rpc_result=None):
        self._tables = {name: list(rows) for name, rows in (tables or {}).items()}
        self._rpc_result = rpc_result if rpc_result is not None else []
        self.rpc_calls: list[tuple[str, dict]] = []
        self.updates: list[tuple[str, dict, dict]] = []

    def table(self, name):
        return _RecordingQuery(self, name, self._tables.get(name, []))

    def rpc(self, fn_name, params):
        self.rpc_calls.append((fn_name, params))
        return SimpleNamespace(execute=lambda: SimpleNamespace(data=self._rpc_result))


class _RecordingQuery(_FakeQuery):
    def __init__(self, parent: _FakeSupabase, table_name: str, rows):
        super().__init__(rows)
        self._parent = parent
        self._table_name = table_name
        self._filters: dict[str, object] = {}

    def eq(self, field, value):
        self._filters[field] = value
        return super().eq(field, value)

    def update(self, payload):
        self._pending_update_payload = payload
        return super().update(payload)

    def execute(self):
        if hasattr(self, "_pending_update_payload"):
            self._parent.updates.append((self._table_name, dict(self._filters), self._pending_update_payload))
            return SimpleNamespace(data=[], count=0)
        return super().execute()


LAUNCH = program.PROGRAM_LAUNCHED_AT
BEFORE_LAUNCH = (LAUNCH - timedelta(days=30)).isoformat()
AFTER_LAUNCH = (LAUNCH + timedelta(hours=1)).isoformat()
WELL_AFTER_LAUNCH = (LAUNCH + timedelta(days=1)).isoformat()


class TestDetectFirstRealAction:
    def test_returns_none_with_no_data_anywhere(self, monkeypatch):
        monkeypatch.setattr(program, "supabase", _FakeSupabase())
        assert program.detect_first_real_action("nobody@example.com", user_id=None) is None

    def test_returns_earliest_checkin(self, monkeypatch):
        fake = _FakeSupabase(
            tables={
                "vt_daily_wellness_entries": [
                    {"email": "user@example.com", "created_at": WELL_AFTER_LAUNCH},
                ]
            }
        )
        monkeypatch.setattr(program, "supabase", fake)
        result = program.detect_first_real_action("user@example.com", user_id=None)
        assert result is not None
        occurred_at, source = result
        assert source == "checkin"

    def test_picks_earliest_across_all_three_sources(self, monkeypatch):
        fake = _FakeSupabase(
            tables={
                "vt_daily_wellness_entries": [{"email": "u@example.com", "created_at": WELL_AFTER_LAUNCH}],
                "vt_twin_calculations": [{"email": "u@example.com", "created_at": AFTER_LAUNCH}],
                "health_sync_runs": [
                    {"user_id": 7, "provider": "health_connect", "status": "completed", "started_at": BEFORE_LAUNCH}
                ],
            }
        )
        monkeypatch.setattr(program, "supabase", fake)
        result = program.detect_first_real_action("u@example.com", user_id=7)
        assert result is not None
        _, source = result
        assert source == "health_connect_sync"

    def test_ignores_health_connect_sync_without_user_id(self, monkeypatch):
        fake = _FakeSupabase(
            tables={
                "health_sync_runs": [
                    {"user_id": 7, "provider": "health_connect", "status": "completed", "started_at": BEFORE_LAUNCH}
                ],
            }
        )
        monkeypatch.setattr(program, "supabase", fake)
        assert program.detect_first_real_action("u@example.com", user_id=None) is None


class TestMaybeClaimDiscountSlot:
    def test_returns_none_when_no_qualifying_action_exists(self, monkeypatch):
        monkeypatch.setattr(program, "supabase", _FakeSupabase())
        assert program.maybe_claim_discount_slot("nobody@example.com") is None

    def test_non_retroactive_for_users_already_active_before_launch(self, monkeypatch):
        """CRITICAL: an existing user whose earliest action predates the
        program's launch must NEVER be granted a slot, no matter which
        qualifying action they perform next."""
        fake = _FakeSupabase(
            tables={"vt_daily_wellness_entries": [{"email": "veteran@example.com", "created_at": BEFORE_LAUNCH}]}
        )
        monkeypatch.setattr(program, "supabase", fake)
        result = program.maybe_claim_discount_slot("veteran@example.com")
        assert result is None
        assert fake.rpc_calls == []  # never even attempted a claim

    def test_claims_a_slot_for_a_genuinely_new_first_action(self, monkeypatch):
        fake = _FakeSupabase(
            tables={"vt_daily_wellness_entries": [{"email": "newbie@example.com", "created_at": AFTER_LAUNCH}]},
            rpc_result=[{"slot_number": 1, "granted": True}],
        )
        monkeypatch.setattr(program, "supabase", fake)
        monkeypatch.setattr(program, "_create_promotion_code_for_grant", lambda email, slot, expires_at: None)
        result = program.maybe_claim_discount_slot("newbie@example.com")
        assert result == {"slot_number": 1, "granted": True}
        assert fake.rpc_calls[0][0] == "claim_beta_discount_slot"
        assert fake.rpc_calls[0][1]["p_email"] == "newbie@example.com"
        assert fake.rpc_calls[0][1]["p_first_real_usage_source"] == "checkin"

    def test_honest_not_granted_once_slots_are_exhausted(self, monkeypatch):
        fake = _FakeSupabase(
            tables={"vt_daily_wellness_entries": [{"email": "unlucky@example.com", "created_at": AFTER_LAUNCH}]},
            rpc_result=[{"slot_number": None, "granted": False}],
        )
        monkeypatch.setattr(program, "supabase", fake)
        result = program.maybe_claim_discount_slot("unlucky@example.com")
        assert result == {"slot_number": None, "granted": False}

    def test_never_raises_if_the_database_call_fails(self, monkeypatch):
        class _Boom:
            def table(self, name):
                raise RuntimeError("db unreachable")

        monkeypatch.setattr(program, "supabase", _Boom())
        assert program.maybe_claim_discount_slot("anyone@example.com") is None

    def test_passes_a_twelve_month_expiry_to_the_rpc_call(self, monkeypatch):
        fake = _FakeSupabase(
            tables={"vt_daily_wellness_entries": [{"email": "newbie@example.com", "created_at": AFTER_LAUNCH}]},
            rpc_result=[{"slot_number": 1, "granted": True}],
        )
        monkeypatch.setattr(program, "supabase", fake)
        monkeypatch.setattr(program, "_create_promotion_code_for_grant", lambda email, slot, expires_at: None)
        before = datetime.now(timezone.utc)
        program.maybe_claim_discount_slot("newbie@example.com")
        expires_at = datetime.fromisoformat(fake.rpc_calls[0][1]["p_expires_at"])
        delta_days = (expires_at - before).days
        assert 360 <= delta_days <= 366  # ~12 calendar months


class TestExcludedEmails:
    def test_excluded_email_never_claims_a_slot_even_with_qualifying_activity(self, monkeypatch):
        monkeypatch.setenv("BETA_DISCOUNT_EXCLUDED_EMAILS", "info@vitaltwin.de,avdi1205@gmail.com")
        fake = _FakeSupabase(
            tables={"vt_daily_wellness_entries": [{"email": "info@vitaltwin.de", "created_at": AFTER_LAUNCH}]},
            rpc_result=[{"slot_number": 1, "granted": True}],
        )
        monkeypatch.setattr(program, "supabase", fake)
        result = program.maybe_claim_discount_slot("info@vitaltwin.de")
        assert result is None
        assert fake.rpc_calls == []

    def test_exclusion_check_is_case_insensitive(self, monkeypatch):
        monkeypatch.setenv("BETA_DISCOUNT_EXCLUDED_EMAILS", "Info@VitalTwin.de")
        fake = _FakeSupabase(
            tables={"vt_daily_wellness_entries": [{"email": "info@vitaltwin.de", "created_at": AFTER_LAUNCH}]}
        )
        monkeypatch.setattr(program, "supabase", fake)
        result = program.maybe_claim_discount_slot("INFO@VITALTWIN.DE")
        assert result is None
        assert fake.rpc_calls == []

    def test_non_excluded_email_is_unaffected(self, monkeypatch):
        monkeypatch.setenv("BETA_DISCOUNT_EXCLUDED_EMAILS", "info@vitaltwin.de")
        fake = _FakeSupabase(
            tables={"vt_daily_wellness_entries": [{"email": "real-user@example.com", "created_at": AFTER_LAUNCH}]},
            rpc_result=[{"slot_number": 1, "granted": True}],
        )
        monkeypatch.setattr(program, "supabase", fake)
        monkeypatch.setattr(program, "_create_promotion_code_for_grant", lambda email, slot, expires_at: None)
        result = program.maybe_claim_discount_slot("real-user@example.com")
        assert result == {"slot_number": 1, "granted": True}

    def test_empty_env_var_excludes_nobody(self, monkeypatch):
        monkeypatch.delenv("BETA_DISCOUNT_EXCLUDED_EMAILS", raising=False)
        assert program._excluded_emails() == set()


class TestExpiration:
    def test_granted_grant_within_window_stays_granted(self, monkeypatch):
        future = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        fake = _FakeSupabase(tables={program.GRANTS_TABLE: [{"email": "u@example.com", "status": "granted", "expires_at": future}]})
        monkeypatch.setattr(program, "supabase", fake)
        grant = program.get_discount_grant_for_email("u@example.com")
        assert grant["status"] == "granted"
        assert fake.updates == []

    def test_granted_grant_past_expiry_lazily_flips_to_expired(self, monkeypatch):
        past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        fake = _FakeSupabase(tables={program.GRANTS_TABLE: [{"email": "u@example.com", "status": "granted", "expires_at": past}]})
        monkeypatch.setattr(program, "supabase", fake)
        grant = program.get_discount_grant_for_email("u@example.com")
        assert grant["status"] == "expired"
        assert len(fake.updates) == 1
        table_name, filters, payload = fake.updates[0]
        assert table_name == program.GRANTS_TABLE
        assert filters["email"] == "u@example.com"
        assert filters["status"] == "granted"
        assert payload["status"] == "expired"

    def test_applied_grant_is_never_touched_by_expiry_even_if_past_due(self, monkeypatch):
        past = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()
        fake = _FakeSupabase(tables={program.GRANTS_TABLE: [{"email": "u@example.com", "status": "applied", "expires_at": past}]})
        monkeypatch.setattr(program, "supabase", fake)
        grant = program.get_discount_grant_for_email("u@example.com")
        assert grant["status"] == "applied"
        assert fake.updates == []

    def test_revoked_grant_is_never_touched_by_expiry(self, monkeypatch):
        past = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()
        fake = _FakeSupabase(tables={program.GRANTS_TABLE: [{"email": "u@example.com", "status": "revoked", "expires_at": past}]})
        monkeypatch.setattr(program, "supabase", fake)
        grant = program.get_discount_grant_for_email("u@example.com")
        assert grant["status"] == "revoked"
        assert fake.updates == []

    def test_expired_grant_never_returns_a_usable_promotion_code(self, monkeypatch):
        past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        fake = _FakeSupabase(
            tables={
                program.GRANTS_TABLE: [
                    {"email": "u@example.com", "status": "granted", "expires_at": past, "stripe_promotion_code_id": "promo_1"}
                ]
            }
        )
        monkeypatch.setattr(program, "supabase", fake)
        assert program.get_unused_promotion_code("u@example.com") is None


class TestGetUnusedPromotionCode:
    def test_none_when_no_grant_exists(self, monkeypatch):
        monkeypatch.setattr(program, "get_discount_grant_for_email", lambda email: None)
        assert program.get_unused_promotion_code("nobody@example.com") is None

    def test_none_once_already_applied(self, monkeypatch):
        monkeypatch.setattr(
            program, "get_discount_grant_for_email", lambda email: {"status": "applied", "stripe_promotion_code_id": "promo_1"}
        )
        assert program.get_unused_promotion_code("used@example.com") is None

    def test_returns_code_when_granted_and_unused(self, monkeypatch):
        monkeypatch.setattr(
            program, "get_discount_grant_for_email", lambda email: {"status": "granted", "stripe_promotion_code_id": "promo_2"}
        )
        assert program.get_unused_promotion_code("fresh@example.com") == "promo_2"


class TestMarkGrantApplied:
    def test_only_updates_rows_still_in_granted_status(self, monkeypatch):
        fake = _FakeSupabase()
        monkeypatch.setattr(program, "supabase", fake)
        program.mark_grant_applied("someone@example.com")
        assert len(fake.updates) == 1
        table_name, filters, payload = fake.updates[0]
        assert table_name == program.GRANTS_TABLE
        assert filters["email"] == "someone@example.com"
        assert filters["status"] == "granted"
        assert payload["status"] == "applied"
        assert "applied_at" in payload

    def test_never_raises_on_db_error(self, monkeypatch):
        class _Boom:
            def table(self, name):
                raise RuntimeError("db down")

        monkeypatch.setattr(program, "supabase", _Boom())
        program.mark_grant_applied("someone@example.com")  # must not raise


class TestStripeCouponAndPromotionCode:
    def test_skips_if_stripe_not_configured(self, monkeypatch):
        import stripe

        monkeypatch.setattr(stripe, "api_key", None)
        assert program._ensure_shared_coupon_exists() is None
        assert program._create_promotion_code_for_grant("x@example.com", 1, datetime.now(timezone.utc)) is None

    def test_reuses_existing_coupon(self, monkeypatch):
        import stripe

        monkeypatch.setattr(stripe, "api_key", "sk_test_fake")
        monkeypatch.setattr(stripe.Coupon, "retrieve", lambda coupon_id: {"id": coupon_id})
        created = {"called": False}

        def _fail_if_called(**kwargs):
            created["called"] = True
            raise AssertionError("should not create a new coupon when one already exists")

        monkeypatch.setattr(stripe.Coupon, "create", _fail_if_called)
        result = program._ensure_shared_coupon_exists()
        assert result == program.SHARED_COUPON_ID
        assert created["called"] is False

    def test_creates_coupon_when_missing(self, monkeypatch):
        import stripe

        monkeypatch.setattr(stripe, "api_key", "sk_test_fake")

        def _raise_not_found(coupon_id):
            raise stripe.InvalidRequestError("No such coupon", param="id")

        monkeypatch.setattr(stripe.Coupon, "retrieve", _raise_not_found)
        captured = {}

        def _create(**kwargs):
            captured.update(kwargs)
            return {"id": kwargs["id"]}

        monkeypatch.setattr(stripe.Coupon, "create", _create)
        result = program._ensure_shared_coupon_exists()
        assert result == program.SHARED_COUPON_ID
        assert captured["percent_off"] == 50
        assert captured["duration"] == "repeating"
        assert captured["duration_in_months"] == 6

    def test_creates_a_restricted_single_use_promotion_code(self, monkeypatch):
        import stripe

        monkeypatch.setattr(stripe, "api_key", "sk_test_fake")
        monkeypatch.setattr(program, "_ensure_shared_coupon_exists", lambda: program.SHARED_COUPON_ID)
        captured = {}

        def _create(**kwargs):
            captured.update(kwargs)
            return {"id": "promo_abc"}

        monkeypatch.setattr(stripe.PromotionCode, "create", _create)
        result = program._create_promotion_code_for_grant("grantee@example.com", 5, datetime.now(timezone.utc))
        assert result == "promo_abc"
        assert captured["promotion"] == {"type": "coupon", "coupon": program.SHARED_COUPON_ID}
        assert captured["max_redemptions"] == 1


class TestListDiscountGrants:
    def test_returns_empty_list_on_db_error(self, monkeypatch):
        class _Boom:
            def table(self, name):
                raise RuntimeError("db down")

        monkeypatch.setattr(program, "supabase", _Boom())
        assert program.list_discount_grants() == []

    def test_returns_all_rows(self, monkeypatch):
        fake = _FakeSupabase(tables={program.GRANTS_TABLE: [{"slot_number": 2}, {"slot_number": 1}]})
        monkeypatch.setattr(program, "supabase", fake)
        result = program.list_discount_grants()
        assert len(result) == 2


class TestRealGrantsExcludeQaTestAccounts:
    def _grants_and_users(self):
        grants = [
            {"email": "qa-test-screenshot-demo@example.com", "created_at": "2026-08-20T00:00:00+00:00", "slot_number": 1},
            {"email": "real-one@example.com", "created_at": "2026-08-21T00:00:00+00:00", "slot_number": 2},
            {"email": "real-two@example.com", "created_at": "2026-08-22T00:00:00+00:00", "slot_number": 3},
        ]
        users = [
            {"email": "qa-test-screenshot-demo@example.com", "full_name": "QA TEST ACCOUNT Screenshot Demo"},
            {"email": "real-one@example.com", "full_name": "Real One"},
            {"email": "real-two@example.com", "full_name": "Real Two"},
        ]
        return grants, users

    def test_count_real_claimed_slots_excludes_test_account(self, monkeypatch):
        grants, users = self._grants_and_users()
        fake = _FakeSupabase(tables={program.GRANTS_TABLE: grants, "vt_users": users})
        monkeypatch.setattr(program, "supabase", fake)
        assert program.count_real_claimed_slots() == 2

    def test_compute_public_rank_skips_test_account_and_ranks_by_created_at(self, monkeypatch):
        grants, users = self._grants_and_users()
        fake = _FakeSupabase(tables={program.GRANTS_TABLE: grants, "vt_users": users})
        monkeypatch.setattr(program, "supabase", fake)
        assert program.compute_public_rank("real-one@example.com") == 1
        assert program.compute_public_rank("real-two@example.com") == 2

    def test_compute_public_rank_returns_none_for_the_test_account_itself(self, monkeypatch):
        grants, users = self._grants_and_users()
        fake = _FakeSupabase(tables={program.GRANTS_TABLE: grants, "vt_users": users})
        monkeypatch.setattr(program, "supabase", fake)
        assert program.compute_public_rank("qa-test-screenshot-demo@example.com") is None

    def test_compute_public_rank_returns_none_for_an_email_with_no_grant(self, monkeypatch):
        grants, users = self._grants_and_users()
        fake = _FakeSupabase(tables={program.GRANTS_TABLE: grants, "vt_users": users})
        monkeypatch.setattr(program, "supabase", fake)
        assert program.compute_public_rank("nobody@example.com") is None

    def test_never_raises_on_db_error(self, monkeypatch):
        class _Boom:
            def table(self, name):
                raise RuntimeError("db down")

        monkeypatch.setattr(program, "supabase", _Boom())
        assert program.count_real_claimed_slots() == 0
        assert program.compute_public_rank("anyone@example.com") is None
