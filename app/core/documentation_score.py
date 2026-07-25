"""Auto Documentation — Documentation Score & Automation Score (VitalTwin
Enterprise, Founder Operating System, Submodule I).

Both scores are computed from real registry/scan/run data — never a
fixed or invented percentage.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from . import documentation_scanner as scanner
from .supabase import supabase

REGISTRY_TABLE = "vt_documentation_registry"
RUN_TABLE = "vt_documentation_generation_runs"


def compute_documentation_score() -> dict:
    try:
        documents = supabase.table(REGISTRY_TABLE).select("*").execute().data or []
    except Exception:
        documents = []

    documented_sources_by_category: dict[str, set[str]] = {}
    for doc in documents:
        documented_sources_by_category.setdefault(doc.get("category"), set()).update(doc.get("source_files") or [])

    api_routes = scanner.scan_api_routes()
    tables = scanner.scan_data_models()
    migrations = scanner.scan_migrations()

    api_sources = documented_sources_by_category.get("api", set())
    documented_apis = len({r["router_file"] for r in api_routes if r["router_file"] in api_sources})
    total_api_files = len({r["router_file"] for r in api_routes})

    model_sources = documented_sources_by_category.get("datenmodelle", set())
    documented_models = len([t for t in tables if t["migration_file"] in model_sources])

    migration_sources = documented_sources_by_category.get("migrationen", set())
    documented_migrations = len([m for m in migrations if m["file"] in migration_sources])

    stale_count = sum(1 for d in documents if d.get("status") == "stale")
    missing_count = 0  # computed separately via documentation_stale_detection.detect_missing_documentation() by the caller, kept out here to avoid a circular import
    open_approvals = 0
    try:
        proposals = supabase.table("vt_documentation_change_proposals").select("status").execute().data or []
        open_approvals = sum(1 for p in proposals if p.get("status") == "offen")
    except Exception:
        pass

    per_category = []
    for category, total, documented in (
        ("api", total_api_files, documented_apis),
        ("datenmodelle", len(tables), documented_models),
        ("migrationen", len(migrations), documented_migrations),
    ):
        per_category.append({
            "category": category, "total": total, "documented": documented,
            "coverage_pct": round(documented / total * 100) if total else None,
        })

    total_artifacts = total_api_files + len(tables) + len(migrations)
    total_documented = documented_apis + documented_models + documented_migrations
    overall_pct = round(total_documented / total_artifacts * 100) if total_artifacts else None

    return {
        "overall_percentage": overall_pct,
        "per_category": per_category,
        "stale_documents": stale_count,
        "missing_documents_hint": missing_count,
        "open_change_proposals": open_approvals,
        "total_registry_entries": len(documents),
        "note": "Berechnet aus vt_documentation_registry + Live-Scan (Routen/Tabellen/Migrationen) — kein fester Wert." if documents or total_artifacts else "Noch keine Dokumentations- oder Scan-Daten vorhanden.",
    }


def compute_documentation_automation_score() -> dict:
    try:
        runs = supabase.table(RUN_TABLE).select("*").order("created_at", desc=True).limit(200).execute().data or []
    except Exception:
        runs = []
    try:
        documents = supabase.table(REGISTRY_TABLE).select("is_generated,requires_approval,status").execute().data or []
    except Exception:
        documents = []

    auto_generated = sum(1 for d in documents if d.get("is_generated"))
    manually_managed = sum(1 for d in documents if not d.get("is_generated"))
    approval_pflichtig = sum(1 for d in documents if d.get("requires_approval"))
    failed_runs = sum(1 for r in runs if r.get("status") == "fehlgeschlagen")
    successful_runs = sum(1 for r in runs if r.get("status") == "erfolgreich")

    total = auto_generated + manually_managed
    automation_pct = round(auto_generated / total * 100) if total else None

    return {
        "auto_detected_changes": successful_runs,
        "auto_generated_drafts": auto_generated,
        "manually_reviewed_documents": manually_managed,
        "approval_required_documents": approval_pflichtig,
        "failed_runs": failed_runs,
        "automation_percentage": automation_pct,
        "note": "Berechnet aus vt_documentation_generation_runs + vt_documentation_registry — kein fester Wert." if runs or documents else "Noch keine Prozessdaten vorhanden.",
    }
