"""Tests for `app.core.learning_events.record_learning_event`.

Mirrors the existing (undocumented but consistent) contract of
`record_audit_event`: a best-effort, fire-and-forget write that must never
raise, even if the underlying Supabase call fails — a failed learning-event
write must never block the actual request that triggered it."""

from __future__ import annotations

from app.core import learning_events as learning_events_module


class _RaisingTable:
    def insert(self, *args, **kwargs):
        raise RuntimeError("simulated network failure")


class _RaisingSupabase:
    def table(self, name):
        return _RaisingTable()


class _RecordingTable:
    def __init__(self, store):
        self._store = store

    def insert(self, payload):
        self._store.append(payload)
        return self

    def execute(self):
        return self


class _RecordingSupabase:
    def __init__(self):
        self.inserted = []

    def table(self, name):
        return _RecordingTable(self.inserted)


class TestRecordLearningEvent:
    def test_never_raises_on_supabase_failure(self, monkeypatch):
        monkeypatch.setattr(learning_events_module, "supabase", _RaisingSupabase())
        # Must not raise.
        learning_events_module.record_learning_event(
            user_id=None,
            email="user-a@example.com",
            event_type="memory_erstellt",
            source_type="twin_memory",
            new_state={"status": "candidate"},
        )

    def test_writes_expected_payload_shape(self, monkeypatch):
        fake = _RecordingSupabase()
        monkeypatch.setattr(learning_events_module, "supabase", fake)
        learning_events_module.record_learning_event(
            user_id=None,
            email="user-a@example.com",
            event_type="muster_verworfen",
            source_type="twin_pattern",
            source_id="p1",
            previous_state={"status": "active"},
            new_state={"status": "discarded"},
            reason="Nicht zutreffend",
        )
        assert len(fake.inserted) == 1
        payload = fake.inserted[0]
        assert payload["event_type"] == "muster_verworfen"
        assert payload["source_type"] == "twin_pattern"
        assert payload["source_id"] == "p1"
        assert payload["previous_state"] == {"status": "active"}
        assert payload["new_state"] == {"status": "discarded"}
        assert payload["reason"] == "Nicht zutreffend"
        assert payload["email"] == "user-a@example.com"
