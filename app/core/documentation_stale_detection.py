"""Auto Documentation — Stale & Missing Documentation Detection (VitalTwin
Enterprise, Founder Operating System, Submodule I).

**Real signal only.** Staleness for backend-scannable categories (`api`,
`datenmodelle`, `migrationen`, `services`) is a genuine source-hash
comparison against `core/documentation_scanner.py`. For entries this
backend cannot open (frontend docs — separate repository), staleness is
the honest, coarser signal "no review recorded in the last 90 days" —
never a fabricated content diff.

Missing documentation is computed by diffing the *live* scanner output
against registry entries — an API/table/service with no matching
registry row is reported as missing, backed by the real scan, not a
static list.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from . import documentation_registry as registry
from . import documentation_scanner as scanner
from .supabase import supabase

REGISTRY_TABLE = "vt_documentation_registry"

STALE_REVIEW_THRESHOLD_DAYS = 90
BACKEND_SCANNABLE_CATEGORIES = frozenset({"api", "datenmodelle", "migrationen", "services"})


def _now() -> datetime:
    return datetime.now(timezone.utc)


def detect_stale_documents() -> list[dict]:
    findings: list[dict] = []
    current_source_hash = scanner.compute_backend_source_hash()

    try:
        documents = supabase.table(REGISTRY_TABLE).select("*").execute().data or []
    except Exception:
        documents = []

    for doc in documents:
        if doc.get("status") in ("archived", "rejected"):
            continue

        if doc.get("category") in BACKEND_SCANNABLE_CATEGORIES and doc.get("is_generated"):
            if doc.get("source_hash") and doc["source_hash"] != current_source_hash:
                reason = "Backend-Quellcode (Routen/Services/Migrationen) hat sich seit der letzten Generierung geändert."
                findings.append({"registry_id": doc["id"], "document_path": doc["document_path"], "reason": reason})
                supabase.table(REGISTRY_TABLE).update({"status": "stale", "stale_reason": reason, "updated_at": _now().isoformat()}).eq("id", doc["id"]).execute()
            continue

        # Frontend / manually-managed documents: coarse age-based signal.
        last_reviewed = doc.get("last_reviewed_at")
        if not last_reviewed:
            continue
        try:
            reviewed_at = datetime.fromisoformat(str(last_reviewed).replace("Z", "+00:00"))
        except ValueError:
            continue
        if (_now() - reviewed_at) > timedelta(days=STALE_REVIEW_THRESHOLD_DAYS):
            reason = f"Kein Review seit über {STALE_REVIEW_THRESHOLD_DAYS} Tagen (nicht automatisch inhaltlich prüfbar — separates Frontend-Repository)."
            findings.append({"registry_id": doc["id"], "document_path": doc["document_path"], "reason": reason})
            supabase.table(REGISTRY_TABLE).update({"status": "stale", "stale_reason": reason, "updated_at": _now().isoformat()}).eq("id", doc["id"]).execute()

    return findings


def detect_missing_documentation() -> list[dict]:
    """Diffs the live scanner output against the registry's `source_files`
    field (a list of backend file names each registry entry claims to
    document) — an artifact with no registry entry referencing its source
    file is reported as missing."""
    missing: list[dict] = []
    try:
        documents = supabase.table(REGISTRY_TABLE).select("category,source_files").execute().data or []
    except Exception:
        documents = []

    documented_sources_by_category: dict[str, set[str]] = {}
    for doc in documents:
        category = doc.get("category")
        sources = doc.get("source_files") or []
        documented_sources_by_category.setdefault(category, set()).update(sources)

    api_sources = documented_sources_by_category.get("api", set())
    for route in scanner.scan_api_routes():
        if route["router_file"] not in api_sources:
            missing.append({"category": "api", "identifier": f"{route['method']} {route['path']}", "source": route["router_file"]})

    model_sources = documented_sources_by_category.get("datenmodelle", set())
    for table in scanner.scan_data_models():
        if table["migration_file"] not in model_sources:
            missing.append({"category": "datenmodelle", "identifier": table["table"], "source": table["migration_file"]})

    migration_sources = documented_sources_by_category.get("migrationen", set())
    for migration in scanner.scan_migrations():
        if migration["file"] not in migration_sources:
            missing.append({"category": "migrationen", "identifier": migration["file"], "source": migration["file"]})

    return missing
