"""Auto Documentation — Generation Orchestration (VitalTwin Enterprise,
Founder Operating System, Submodule I).

Runs the safe scanners, updates non-protected generated registry entries
in place (versioned), flags stale/missing documentation, and records a
`vt_documentation_generation_runs` row for every run — never silently.

**No background scheduler** (consistent with every other Founder-OS
module): a generation run happens when `POST /documentation/generate` is
called, or via the Automation Engine action `dokumentation_pruefen`
(Submodule G integration).
"""

from __future__ import annotations

from datetime import datetime, timezone

from . import documentation_registry as registry
from . import documentation_scanner as scanner
from . import documentation_stale_detection as stale_detection
from .supabase import supabase

RUN_TABLE = "vt_documentation_generation_runs"
TASK_TABLE = "vt_founder_tasks"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _create_or_refresh_task(*, dedupe_key: str, **fields) -> None:
    existing = supabase.table(TASK_TABLE).select("id").eq("dedupe_key", dedupe_key).limit(1).execute().data or []
    if existing:
        return
    try:
        supabase.table(TASK_TABLE).insert({**fields, "dedupe_key": dedupe_key, "auto_detected": True}).execute()
    except Exception:
        pass


def _generate_api_overview_content() -> str:
    routes = scanner.scan_api_routes()
    lines = ["# API-Übersicht (automatisch generiert)", ""]
    for route in routes:
        lines.append(f"- `{route['method']} {route['path']}` — Datei: `{route['router_file']}`, Berechtigung: `{route['permission'] or 'keine (öffentlich oder ungeprüft)'}`")
    return "\n".join(lines)


def _generate_data_model_overview_content() -> str:
    tables = scanner.scan_data_models()
    lines = ["# Datenmodell-Übersicht (automatisch generiert)", ""]
    for table in tables:
        lines.append(f"- `{table['table']}` — definiert in `{table['migration_file']}`")
    return "\n".join(lines)


def _generate_migration_overview_content() -> str:
    migrations = scanner.scan_migrations()
    lines = ["# Migrations-Übersicht (automatisch generiert)", ""]
    for migration in migrations:
        marker = " (neueste)" if migration["is_latest"] else ""
        lines.append(f"- `{migration['file']}`{marker}")
    return "\n".join(lines)


def _generate_service_overview_content() -> str:
    services = scanner.scan_core_services()
    lines = ["# Service-Übersicht (automatisch generiert)", ""]
    for service in services:
        lines.append(f"- `{service['module']}` — {service['purpose']}")
    return "\n".join(lines)


_GENERATED_DOCUMENTS: tuple[dict, ...] = (
    {"document_path": "generated::api_overview", "title": "API-Übersicht", "category": "api", "generator": _generate_api_overview_content, "source_files_fn": lambda: sorted({r["router_file"] for r in scanner.scan_api_routes()})},
    {"document_path": "generated::data_model_overview", "title": "Datenmodell-Übersicht", "category": "datenmodelle", "generator": _generate_data_model_overview_content, "source_files_fn": lambda: sorted({t["migration_file"] for t in scanner.scan_data_models()})},
    {"document_path": "generated::migration_overview", "title": "Migrations-Übersicht", "category": "migrationen", "generator": _generate_migration_overview_content, "source_files_fn": lambda: [m["file"] for m in scanner.scan_migrations()]},
    {"document_path": "generated::service_overview", "title": "Service-Übersicht", "category": "services", "generator": _generate_service_overview_content, "source_files_fn": lambda: [s["module"] for s in scanner.scan_core_services()]},
)


def run_generation(*, run_type: str = "manuell", triggered_by: str | None = None) -> dict:
    started_at = _now_iso()
    run_payload = {"run_type": run_type, "status": "laeuft", "started_at": started_at, "created_by": triggered_by}
    response = supabase.table(RUN_TABLE).insert(run_payload).execute()
    run = response.data[0] if response.data else run_payload

    items_scanned = 0
    items_updated = 0
    error = None

    try:
        for spec in _GENERATED_DOCUMENTS:
            items_scanned += 1
            existing_rows = supabase.table("vt_documentation_registry").select("*").eq("document_path", spec["document_path"]).limit(1).execute().data or []
            content = spec["generator"]()
            source_files = spec["source_files_fn"]()
            source_hash = scanner.compute_backend_source_hash()

            if not existing_rows:
                registry.register_document(
                    {
                        "document_path": spec["document_path"], "title": spec["title"], "category": spec["category"],
                        "module": "founder_os", "submodule": "I", "status": "current", "source_files": source_files,
                        "is_generated": True, "requires_approval": False, "generated_content": content,
                        "content_hash": None, "source_hash": source_hash, "last_generated_at": _now_iso(),
                    },
                    created_by=triggered_by or "auto_documentation",
                )
                items_updated += 1
                continue

            doc = existing_rows[0]
            if doc.get("source_hash") != source_hash or doc.get("generated_content") != content:
                registry.update_document_content(
                    doc["id"], content=content, diff_summary={"regenerated": True}, updated_by=triggered_by, source_hash=source_hash,
                )
                items_updated += 1

        stale_findings = stale_detection.detect_stale_documents()
        for finding in stale_findings:
            _create_or_refresh_task(
                dedupe_key=f"documentation_stale_{finding['registry_id']}",
                title=f"Dokumentation veraltet: {finding['document_path']}", category="technik", source="auto_documentation",
                priority="mittel", status="neu", reason=finding["reason"], data_used=finding["document_path"],
                impact_if_ignored="Dokumentation entspricht nicht mehr dem tatsächlichen Stand.",
                suggested_action_available=False,
            )

        missing_findings = stale_detection.detect_missing_documentation()
        for finding in missing_findings:
            dedupe_key = f"documentation_missing_{finding['category']}_{finding['identifier']}"
            _create_or_refresh_task(
                dedupe_key=dedupe_key, title=f"Fehlende Dokumentation: {finding['identifier']}", category="technik",
                source="auto_documentation", priority="niedrig", status="neu",
                reason=f"Kein Dokumentationseintrag für {finding['category']} '{finding['identifier']}' gefunden.",
                data_used=finding["source"], impact_if_ignored="Neue Entwickler finden keine Dokumentation zu diesem Artefakt.",
                suggested_action_available=False,
            )

    except Exception as exc:  # noqa: BLE001 — a failed run must be reported, never silently swallowed
        error = str(exc)

    finished_at = _now_iso()
    final_status = "fehlgeschlagen" if error else "erfolgreich"
    update_payload = {
        "status": final_status, "items_scanned": items_scanned, "items_updated": items_updated,
        "items_flagged_stale": len(stale_findings) if error is None else 0, "error": error, "finished_at": finished_at,
    }
    supabase.table(RUN_TABLE).update(update_payload).eq("id", run.get("id")).execute()

    if error:
        _create_or_refresh_task(
            dedupe_key=f"documentation_run_failed_{run.get('id')}",
            title="Dokumentationslauf fehlgeschlagen", category="technik", source="auto_documentation",
            priority="hoch", status="neu", reason=error, data_used=f"vt_documentation_generation_runs.id={run.get('id')}",
            impact_if_ignored="Dokumentation bleibt inkonsistent mit dem tatsächlichen Code.", suggested_action_available=False,
        )

    return {**run, **update_payload}
