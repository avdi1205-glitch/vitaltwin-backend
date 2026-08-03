"""Tests for `core.sentry_setup` — inert-unless-configured guard and the
sensitive-header scrub used in `before_send`/`before_send_transaction`."""

from __future__ import annotations

from app.core.sentry_setup import _scrub_sensitive_headers, init_sentry


class TestInitSentry:
    def test_noop_without_dsn(self, monkeypatch):
        monkeypatch.delenv("SENTRY_DSN", raising=False)
        # Must not raise and must not import sentry_sdk's init path.
        init_sentry()

    def test_initializes_when_dsn_set(self, monkeypatch):
        monkeypatch.setenv("SENTRY_DSN", "https://public@o0.ingest.sentry.io/0")
        calls = {}

        class _FakeSentrySdk:
            @staticmethod
            def init(**kwargs):
                calls.update(kwargs)

        import sys

        monkeypatch.setitem(sys.modules, "sentry_sdk", _FakeSentrySdk())
        init_sentry()
        assert calls["dsn"] == "https://public@o0.ingest.sentry.io/0"
        assert calls["send_default_pii"] is False
        assert calls["max_request_body_size"] == "never"


class TestScrubSensitiveHeaders:
    def test_redacts_authorization_and_cookie(self):
        event = {"request": {"headers": {"Authorization": "Bearer secret", "Cookie": "session=abc", "Accept": "json"}}}
        result = _scrub_sensitive_headers(event, {})
        assert result["request"]["headers"]["Authorization"] == "[Filtered]"
        assert result["request"]["headers"]["Cookie"] == "[Filtered]"
        assert result["request"]["headers"]["Accept"] == "json"

    def test_handles_missing_request_gracefully(self):
        assert _scrub_sensitive_headers({}, {}) == {}
