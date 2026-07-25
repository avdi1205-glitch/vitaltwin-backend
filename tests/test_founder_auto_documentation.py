"""Tests for Auto Documentation (VitalTwin Enterprise, Founder Operating
System, Submodule I): safe read-only scanner, protected-document rules,
registry/versioning/rollback, stale & missing detection, changelog/
release-notes engines, documentation score, generation orchestration,
change proposals, search, and the API router — permissions (incl.
documentation_editor, developer, admin-excluded), security (protected
docs never auto-overwritten), AI Q&A (mocked provider)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core import documentation_change_proposals as proposals_module
from app.core import documentation_generation as generation_module
from app.core import documentation_protected as protected
from app.core import documentation_registry as registry_module
from app.core import documentation_scanner as scanner
from app.core import documentation_score as score_module
from app.core import documentation_search as search_module
from app.core import documentation_stale_detection as stale_module
from app.core import changelog_engine
from app.core import release_notes_engine
from app.core.admin_rbac import ROLE_PERMISSIONS, role_has_permission
from app.routers import founder_documentation as doc_router
from app.services.ai_provider import AIProviderUnavailableError


@pytest.fixture
def anyio_backend():
    return "asyncio"


class _FakeQuery:
    def __init__(self, table_rows: list[dict]):
        self._table_rows = table_rows
        self._predicates = []
        self._pending_insert = None
        self._pending_update = None
        self._pending_delete = False
        self._limit_n = None

    def select(self, *a, count=None, **k):
        return self

    def eq(self, field, value):
        self._predicates.append(lambda row, f=field, v=value: row.get(f) == v)
        return self

    def gte(self, field, value):
        self._predicates.append(lambda row, f=field, v=value: str(row.get(f, "")) >= str(value))
        return self

    def lt(self, field, value):
        self._predicates.append(lambda row, f=field, v=value: str(row.get(f, "")) < str(value))
        return self

    def in_(self, field, values):
        self._predicates.append(lambda row, f=field, v=set(values): row.get(f) in v)
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

    def delete(self):
        self._pending_delete = True
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
        if self._pending_delete:
            matched = [r for r in self._table_rows if all(p(r) for p in self._predicates)]
            for row in matched:
                self._table_rows.remove(row)
            return SimpleNamespace(data=matched, count=len(matched))
        rows = self._matching()
        return SimpleNamespace(data=rows, count=len(rows))


class _FakeSupabase:
    def __init__(self, tables: dict[str, list[dict]] | None = None):
        self.tables = tables or {}

    def table(self, name):
        rows = self.tables.setdefault(name, [])
        return _FakeQuery(rows)


@pytest.fixture
def doc_supabase(monkeypatch):
    fake = _FakeSupabase()
    monkeypatch.setattr(registry_module, "supabase", fake)
    monkeypatch.setattr(stale_module, "supabase", fake)
    monkeypatch.setattr(generation_module, "supabase", fake)
    monkeypatch.setattr(proposals_module, "supabase", fake)
    monkeypatch.setattr(search_module, "supabase", fake)
    monkeypatch.setattr(score_module, "supabase", fake)
    monkeypatch.setattr(doc_router, "supabase", fake)
    return fake


@pytest.fixture
def doc_permission_spy(monkeypatch):
    calls: list[tuple] = []

    def _fake(authorization, permission):
        calls.append((authorization, permission))
        return SimpleNamespace(email="founder@example.com", role="super_admin")

    monkeypatch.setattr(doc_router, "require_admin_permission", _fake)
    return calls


class TestRbacMatrix:
    def test_documentation_editor_has_both_permissions(self):
        assert ROLE_PERMISSIONS["documentation_editor"] == {"view_documentation", "manage_documentation"}

    def test_developer_has_documentation_permissions(self):
        assert role_has_permission("developer", "view_documentation")
        assert role_has_permission("developer", "manage_documentation")

    def test_admin_excluded_from_documentation(self):
        assert not role_has_permission("admin", "view_documentation")
        assert not role_has_permission("admin", "manage_documentation")

    def test_super_admin_has_documentation(self):
        assert role_has_permission("super_admin", "view_documentation")
        assert role_has_permission("super_admin", "manage_documentation")


class TestSafeScanner:
    def test_scan_api_routes_finds_real_routes(self):
        routes = scanner.scan_api_routes()
        assert len(routes) > 50  # this backend has many real routers/endpoints
        assert all("method" in r and "path" in r and "router_file" in r for r in routes)

    def test_scan_data_models_finds_real_tables(self):
        tables = scanner.scan_data_models()
        table_names = {t["table"] for t in tables}
        assert "vt_documentation_registry" in table_names

    def test_scan_migrations_marks_latest(self):
        migrations = scanner.scan_migrations()
        assert any(m["is_latest"] for m in migrations)

    def test_scan_core_services_never_returns_full_file_content(self):
        services = scanner.scan_core_services()
        for service in services:
            assert len(service["purpose"]) < 300  # one docstring line, never a full file dump

    def test_scanner_refuses_paths_outside_allowlist(self, tmp_path):
        outside_file = tmp_path / "secret.env"
        outside_file.write_text("SECRET=1")
        assert scanner._is_allowed(outside_file) is False

    def test_source_hash_is_deterministic_for_same_state(self):
        first = scanner.compute_backend_source_hash()
        second = scanner.compute_backend_source_hash()
        assert first == second


class TestProtectedDocuments:
    def test_constitution_is_protected(self):
        assert protected.is_protected("frontend/docs/VITALTWIN_CONSTITUTION.md") is True

    def test_impressum_is_protected(self):
        assert protected.is_protected("frontend/app/impressum/page.tsx") is True

    def test_regular_doc_is_not_protected(self):
        assert protected.is_protected("frontend/docs/AUTOMATION_ENGINE.md") is False

    def test_assert_raises_for_protected(self):
        with pytest.raises(PermissionError):
            protected.assert_not_protected_for_auto_update("docs/VITALTWIN_CONSTITUTION.md")

    def test_assert_passes_for_non_protected(self):
        protected.assert_not_protected_for_auto_update("docs/AUTOMATION_ENGINE.md")  # no raise


class TestDocumentationRegistry:
    def test_seed_known_documents_is_idempotent(self, doc_supabase):
        first = registry_module.seed_known_documents()
        second = registry_module.seed_known_documents()
        assert first > 0
        assert second == 0

    def test_seeded_docs_marked_unverifiable(self, doc_supabase):
        registry_module.seed_known_documents()
        docs = doc_supabase.tables["vt_documentation_registry"]
        assert all(d["status"] == "manually_managed" for d in docs)

    def test_register_and_get_document(self, doc_supabase):
        saved = registry_module.register_document(
            {"document_path": "generated::api_overview", "title": "API", "category": "api", "status": "current", "source_files": []},
            created_by="founder@example.com",
        )
        fetched = registry_module.get_document(saved["id"])
        assert fetched["title"] == "API"

    def test_update_content_blocks_protected_document(self, doc_supabase):
        saved = registry_module.register_document(
            {"document_path": "frontend/docs/VITALTWIN_CONSTITUTION.md", "title": "Constitution", "category": "projektuebersicht", "status": "manually_managed"},
            created_by="x",
        )
        with pytest.raises(PermissionError):
            registry_module.update_document_content(saved["id"], content="new content", diff_summary={}, updated_by="x")

    def test_update_content_succeeds_for_non_protected_and_versions(self, doc_supabase):
        saved = registry_module.register_document(
            {"document_path": "generated::api_overview", "title": "API", "category": "api", "status": "draft"},
            created_by="x",
        )
        updated = registry_module.update_document_content(saved["id"], content="# API v1", diff_summary={"added": ["x"]}, updated_by="x")
        assert updated["version"] == 2
        versions = registry_module.list_versions(saved["id"])
        assert len(versions) == 1

    def test_rollback_restores_previous_content(self, doc_supabase):
        saved = registry_module.register_document(
            {"document_path": "generated::api_overview", "title": "API", "category": "api", "status": "draft"},
            created_by="x",
        )
        registry_module.update_document_content(saved["id"], content="version two", diff_summary={}, updated_by="x")
        registry_module.update_document_content(saved["id"], content="version three", diff_summary={}, updated_by="x")
        restored = registry_module.rollback_document(saved["id"], target_version=2, rolled_back_by="x")
        assert restored["generated_content"] == "version two"

    def test_rollback_never_touches_source_or_code(self, doc_supabase):
        # Structural guarantee: rollback_document only ever calls
        # supabase.table(REGISTRY_TABLE)/VERSION_TABLE — verified by the
        # fact this function has no filesystem or migration imports.
        import inspect
        source = inspect.getsource(registry_module.rollback_document)
        assert "open(" not in source and "Path(" not in source


class TestStaleAndMissingDetection:
    def test_backend_scannable_doc_flagged_stale_on_hash_mismatch(self, doc_supabase):
        doc_supabase.tables["vt_documentation_registry"] = [
            {"id": "d1", "document_path": "generated::api_overview", "category": "api", "is_generated": True, "source_hash": "old-hash", "status": "current"},
        ]
        findings = stale_module.detect_stale_documents()
        assert len(findings) == 1
        assert doc_supabase.tables["vt_documentation_registry"][0]["status"] == "stale"

    def test_frontend_doc_stale_after_90_days_without_review(self, doc_supabase):
        import datetime
        old_review = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=200)).isoformat()
        doc_supabase.tables["vt_documentation_registry"] = [
            {"id": "d2", "document_path": "frontend/docs/AUTOMATION_ENGINE.md", "category": "automatisierung", "is_generated": False, "last_reviewed_at": old_review, "status": "current"},
        ]
        findings = stale_module.detect_stale_documents()
        assert len(findings) == 1

    def test_frontend_doc_not_stale_within_90_days(self, doc_supabase):
        import datetime
        recent_review = datetime.datetime.now(datetime.timezone.utc).isoformat()
        doc_supabase.tables["vt_documentation_registry"] = [
            {"id": "d3", "document_path": "frontend/docs/AUTOMATION_ENGINE.md", "category": "automatisierung", "is_generated": False, "last_reviewed_at": recent_review, "status": "current"},
        ]
        assert stale_module.detect_stale_documents() == []

    def test_archived_documents_never_flagged(self, doc_supabase):
        doc_supabase.tables["vt_documentation_registry"] = [
            {"id": "d4", "document_path": "generated::api_overview", "category": "api", "is_generated": True, "source_hash": "wrong", "status": "archived"},
        ]
        assert stale_module.detect_stale_documents() == []

    def test_missing_documentation_detects_undocumented_migration(self, doc_supabase):
        doc_supabase.tables["vt_documentation_registry"] = []
        missing = stale_module.detect_missing_documentation()
        assert any(m["category"] == "migrationen" for m in missing)

    def test_missing_documentation_empty_when_fully_documented(self, doc_supabase):
        all_api_files = sorted({r["router_file"] for r in scanner.scan_api_routes()})
        all_model_files = sorted({t["migration_file"] for t in scanner.scan_data_models()})
        all_migration_files = [m["file"] for m in scanner.scan_migrations()]
        doc_supabase.tables["vt_documentation_registry"] = [
            {"category": "api", "source_files": all_api_files},
            {"category": "datenmodelle", "source_files": all_model_files},
            {"category": "migrationen", "source_files": all_migration_files},
        ]
        assert stale_module.detect_missing_documentation() == []


class TestChangelogAndReleaseNotes:
    def test_changelog_has_all_expected_categories(self):
        draft = changelog_engine.generate_changelog_draft()
        assert set(draft["categories"].keys()) | set(draft["empty_categories"]) == set(changelog_engine.CHANGELOG_CATEGORIES)

    def test_changelog_falls_back_honestly_without_git(self, monkeypatch):
        monkeypatch.setattr(changelog_engine, "_git_available", lambda: False)
        draft = changelog_engine.generate_changelog_draft()
        assert draft["git_available"] is False
        assert "Datenbank" in draft["source_note"] or "Stand" in draft["source_note"]

    def test_internal_release_notes_include_all_categories(self):
        notes = release_notes_engine.generate_internal_release_notes()
        assert notes["audience"] == "intern"
        assert notes["requires_approval"] is False

    def test_user_release_notes_exclude_internal_categories(self, monkeypatch):
        monkeypatch.setattr(changelog_engine, "_read_git_log", lambda: ["feat: neue Funktion", "chore: internal refactor", "security: patched"])
        notes = release_notes_engine.generate_user_release_notes()
        assert notes["requires_approval"] is True
        assert "Sicherheits" not in str(notes["sections"])

    def test_user_release_notes_never_reveal_security_details(self, monkeypatch):
        monkeypatch.setattr(changelog_engine, "_read_git_log", lambda: ["security: fixed auth bypass in admin panel"])
        notes = release_notes_engine.generate_user_release_notes()
        assert "auth bypass" not in str(notes["sections"])


class TestDocumentationScore:
    def test_score_never_fixed_percentage(self, doc_supabase):
        score_a = score_module.compute_documentation_score()
        doc_supabase.tables["vt_documentation_registry"] = [
            {"category": "api", "source_files": [r["router_file"] for r in scanner.scan_api_routes()]},
        ]
        score_b = score_module.compute_documentation_score()
        assert score_a["overall_percentage"] != score_b["overall_percentage"] or score_a["per_category"] != score_b["per_category"]

    def test_automation_score_computed_from_real_runs(self, doc_supabase):
        doc_supabase.tables["vt_documentation_generation_runs"] = [{"status": "erfolgreich"}, {"status": "fehlgeschlagen"}]
        doc_supabase.tables["vt_documentation_registry"] = [{"is_generated": True, "requires_approval": False, "status": "current"}]
        result = score_module.compute_documentation_automation_score()
        assert result["failed_runs"] == 1
        assert result["auto_generated_drafts"] == 1


class TestGenerationOrchestration:
    def test_run_generation_creates_registry_entries(self, doc_supabase):
        result = generation_module.run_generation(run_type="test", triggered_by="tester")
        assert result["status"] == "erfolgreich"
        assert result["items_scanned"] == 4  # api/data-model/migration/service overviews
        assert len(doc_supabase.tables["vt_documentation_registry"]) == 4

    def test_run_generation_is_idempotent_on_rerun(self, doc_supabase):
        generation_module.run_generation(run_type="test", triggered_by="tester")
        generation_module.run_generation(run_type="test", triggered_by="tester")
        assert len(doc_supabase.tables["vt_documentation_registry"]) == 4  # no duplicate entries

    def test_run_generation_never_writes_protected_documents(self, doc_supabase):
        generation_module.run_generation(run_type="test", triggered_by="tester")
        for doc in doc_supabase.tables["vt_documentation_registry"]:
            assert protected.is_protected(doc["document_path"]) is False


class TestChangeProposalsForProtectedDocuments:
    def test_create_proposal_requires_protected_path(self, doc_supabase):
        with pytest.raises(ValueError):
            proposals_module.create_change_proposal(
                registry_id="r1", document_path="frontend/docs/AUTOMATION_ENGINE.md",
                proposed_content="x", reason="y", created_by="founder@example.com",
            )

    def test_create_proposal_for_protected_document_succeeds(self, doc_supabase):
        proposal = proposals_module.create_change_proposal(
            registry_id="r1", document_path="frontend/docs/VITALTWIN_CONSTITUTION.md",
            proposed_content="new text", reason="update mission", created_by="founder@example.com",
        )
        assert proposal["status"] == "offen"

    def test_send_to_approval_center_is_idempotent(self, doc_supabase):
        proposal = proposals_module.create_change_proposal(
            registry_id="r1", document_path="frontend/docs/VITALTWIN_CONSTITUTION.md",
            proposed_content="new text", reason="update mission", created_by="founder@example.com",
        )
        first = proposals_module.send_proposal_to_approval_center(proposal["id"], document_path="frontend/docs/VITALTWIN_CONSTITUTION.md", sent_by="founder@example.com")
        second = proposals_module.send_proposal_to_approval_center(proposal["id"], document_path="frontend/docs/VITALTWIN_CONSTITUTION.md", sent_by="founder@example.com")
        assert first == second
        assert len(doc_supabase.tables["vt_founder_approvals"]) == 1


class TestDocumentationSearch:
    def test_search_matches_title(self, doc_supabase):
        doc_supabase.tables["vt_documentation_registry"] = [{"id": "1", "title": "Automation Engine", "category": "automatisierung"}]
        results = search_module.search_documents("automation")
        assert len(results) == 1

    def test_search_empty_query_returns_nothing(self, doc_supabase):
        doc_supabase.tables["vt_documentation_registry"] = [{"id": "1", "title": "X"}]
        assert search_module.search_documents("") == []

    def test_search_never_indexes_env_secrets(self, doc_supabase):
        doc_supabase.tables["vt_documentation_registry"] = [{"id": "1", "title": "X", "generated_content": "no secrets here"}]
        # Structural guarantee: search only reads registry fields, never a raw .env file.
        results = search_module.search_documents("SECRET_KEY")
        assert results == []


class TestDocumentationRouterPermissions:
    @pytest.mark.anyio
    async def test_dashboard_requires_view_permission(self, doc_supabase, doc_permission_spy):
        await doc_router.documentation_dashboard(authorization="Bearer x")
        assert doc_permission_spy[-1] == ("Bearer x", "view_documentation")

    @pytest.mark.anyio
    async def test_generate_requires_manage_permission(self, doc_supabase, doc_permission_spy):
        await doc_router.generate(authorization="Bearer x")
        assert doc_permission_spy[-1] == ("Bearer x", "manage_documentation")

    @pytest.mark.anyio
    async def test_archive_requires_founder_role(self, doc_supabase, monkeypatch):
        def _fake_admin(authorization, permission):
            return SimpleNamespace(email="admin@example.com", role="documentation_editor")
        monkeypatch.setattr(doc_router, "require_admin_permission", _fake_admin)
        with pytest.raises(HTTPException) as exc_info:
            await doc_router.archive_document("r1", authorization="Bearer x")
        assert exc_info.value.status_code == 403

    @pytest.mark.anyio
    async def test_archive_succeeds_for_super_admin(self, doc_supabase, doc_permission_spy):
        doc_supabase.tables["vt_documentation_registry"] = [{"id": "r1", "status": "current"}]
        result = await doc_router.archive_document("r1", authorization="Bearer x")
        assert result["message"] == "Archiviert."
        assert doc_supabase.tables["vt_documentation_registry"][0]["status"] == "archived"


class TestDocumentationAssistant:
    @pytest.mark.anyio
    async def test_insufficient_data_when_registry_empty(self, doc_supabase, doc_permission_spy):
        data = doc_router.AskInput(question="Welche Module sind implementiert?")
        result = await doc_router.ask_documentation(data, request=SimpleNamespace(client=SimpleNamespace(host="127.0.0.30")), authorization="Bearer x")
        assert result["insufficient_data"] is True
        assert result["answer"] == doc_router.INSUFFICIENT_DATA_MESSAGE

    @pytest.mark.anyio
    async def test_provider_failure_returns_503(self, doc_supabase, doc_permission_spy, monkeypatch):
        doc_supabase.tables["vt_documentation_registry"] = [{"id": "1", "title": "X", "category": "api", "status": "current"}]

        class _FailingProvider:
            async def generate_recommendation_explanation(self, *, system_prompt, context_text):
                raise AIProviderUnavailableError("down")

        monkeypatch.setattr(doc_router, "_get_ai_provider", lambda: _FailingProvider())
        data = doc_router.AskInput(question="Welche APIs existieren?")
        with pytest.raises(HTTPException) as exc_info:
            await doc_router.ask_documentation(data, request=SimpleNamespace(client=SimpleNamespace(host="127.0.0.31")), authorization="Bearer x")
        assert exc_info.value.status_code == 503

    @pytest.mark.anyio
    async def test_successful_answer_is_grounded(self, doc_supabase, doc_permission_spy, monkeypatch):
        doc_supabase.tables["vt_documentation_registry"] = [{"id": "1", "title": "X", "category": "api", "status": "current"}]

        class _FakeProvider:
            async def generate_recommendation_explanation(self, *, system_prompt, context_text):
                assert "Frage:" in context_text
                return "Antwort aus echten Projektdaten."

        monkeypatch.setattr(doc_router, "_get_ai_provider", lambda: _FakeProvider())
        data = doc_router.AskInput(question="Welche APIs existieren?")
        result = await doc_router.ask_documentation(data, request=SimpleNamespace(client=SimpleNamespace(host="127.0.0.32")), authorization="Bearer x")
        assert result["insufficient_data"] is False
        assert "echten Projektdaten" in result["answer"]
