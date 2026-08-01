"""Unit tests for `app.core.ai_usage_logger` — the central AI usage log
(Founder OS internal foundation #1 + #2). Mocks Supabase — no real network."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.core import ai_usage_logger


class _FakeQuery:
    def __init__(self, store: list):
        self._store = store
        self._filters: list[tuple] = []

    def insert(self, payload):
        self._store.append(payload)
        return self

    def select(self, *args, **kwargs):
        return self

    def gte(self, *args, **kwargs):
        return self

    def order(self, *args, **kwargs):
        return self

    def limit(self, *args, **kwargs):
        return self

    def execute(self):
        return SimpleNamespace(data=list(self._store), count=len(self._store))


class _FakeSupabase:
    def __init__(self):
        self.rows: list = []

    def table(self, name):
        assert name == "vt_ai_usage_events"
        return _FakeQuery(self.rows)


@pytest.fixture
def fake_supabase(monkeypatch):
    fake = _FakeSupabase()
    monkeypatch.setattr(ai_usage_logger, "supabase", fake)
    return fake


class TestLogAiUsageSuccess:
    def test_logs_tokens_without_cost_when_pricing_not_configured(self, fake_supabase, monkeypatch):
        monkeypatch.delenv("OPENAI_PROMPT_PRICE_PER_1K_USD", raising=False)
        monkeypatch.delenv("OPENAI_COMPLETION_PRICE_PER_1K_USD", raising=False)

        ai_usage_logger.log_ai_usage(
            feature="twin_chat", email="user@example.com", model="gpt-4o-mini",
            usage={"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
            latency_ms=250,
        )

        assert len(fake_supabase.rows) == 1
        row = fake_supabase.rows[0]
        assert row["prompt_tokens"] == 100
        assert row["completion_tokens"] == 50
        assert row["total_tokens"] == 150
        assert row["cost_usd"] is None
        assert "OPENAI_PROMPT_PRICE_PER_1K_USD" in row["cost_note"]

    def test_computes_real_cost_when_pricing_configured(self, fake_supabase, monkeypatch):
        monkeypatch.setenv("OPENAI_PROMPT_PRICE_PER_1K_USD", "0.15")
        monkeypatch.setenv("OPENAI_COMPLETION_PRICE_PER_1K_USD", "0.60")

        ai_usage_logger.log_ai_usage(
            feature="twin_chat", model="gpt-4o-mini",
            usage={"prompt_tokens": 1000, "completion_tokens": 1000, "total_tokens": 2000},
        )

        row = fake_supabase.rows[0]
        assert row["cost_usd"] == pytest.approx(0.75)
        assert row["cost_note"] is None


class TestLogAiUsageError:
    def test_logs_error_status_with_no_tokens(self, fake_supabase):
        ai_usage_logger.log_ai_usage(feature="twin_chat", status="error", error_type="AIProviderTimeoutError")
        row = fake_supabase.rows[0]
        assert row["status"] == "error"
        assert row["error_type"] == "AIProviderTimeoutError"
        assert row["prompt_tokens"] is None


class TestLogAiUsageNeverRaises:
    def test_swallows_supabase_exception(self, monkeypatch):
        class _BrokenSupabase:
            def table(self, name):
                raise RuntimeError("boom")

        monkeypatch.setattr(ai_usage_logger, "supabase", _BrokenSupabase())
        ai_usage_logger.log_ai_usage(feature="twin_chat")  # must not raise


class TestGetAiUsageSummary:
    def test_aggregates_requests_errors_tokens(self, fake_supabase):
        fake_supabase.rows.extend([
            {"status": "success", "total_tokens": 100, "cost_usd": 0.01, "latency_ms": 200},
            {"status": "error", "error_type": "AIProviderTimeoutError", "total_tokens": None, "cost_usd": None, "latency_ms": 100},
        ])
        summary = ai_usage_logger.get_ai_usage_summary(days=1)
        assert summary["requests"] == 2
        assert summary["errors"] == 1
        assert summary["total_tokens"] == 100
        assert summary["cost_usd"] == pytest.approx(0.01)
        assert summary["avg_latency_ms"] == 150

    def test_returns_none_summary_when_table_unreachable(self, monkeypatch):
        class _BrokenSupabase:
            def table(self, name):
                raise RuntimeError("boom")

        monkeypatch.setattr(ai_usage_logger, "supabase", _BrokenSupabase())
        summary = ai_usage_logger.get_ai_usage_summary(days=1)
        assert summary["requests"] is None
        assert summary["cost_note"]

    def test_zero_requests_is_a_real_zero_not_none(self, fake_supabase):
        summary = ai_usage_logger.get_ai_usage_summary(days=1)
        assert summary["requests"] == 0
        assert summary["total_tokens"] == 0
