"""Auto Documentation — Documentation Search (VitalTwin Enterprise,
Founder Operating System, Submodule I).

Searches only already-registered, non-secret registry metadata and the
`generated_content` field this backend itself produced — never scans
arbitrary files, never indexes `.env`/secrets.
"""

from __future__ import annotations

from .supabase import supabase

REGISTRY_TABLE = "vt_documentation_registry"


def search_documents(query: str) -> list[dict]:
    needle = (query or "").strip().lower()
    if not needle:
        return []
    try:
        documents = supabase.table(REGISTRY_TABLE).select("*").execute().data or []
    except Exception:
        documents = []

    results = []
    for doc in documents:
        haystack = " ".join(
            str(doc.get(field, "")) for field in
            ("title", "module", "submodule", "category", "status", "document_path", "version", "updated_at", "generated_content")
        ).lower()
        if needle in haystack:
            results.append(doc)
    return results
