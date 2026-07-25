"""Auto Documentation — Protected Document Rules (VitalTwin Enterprise,
Founder Operating System, Submodule I).

Documents matching `is_protected()` may **never** have their content
auto-generated or auto-overwritten by this module — only a change
*proposal* can be prepared, which always requires a founder decision via
the Smart Approval Center (Submodule D, reused directly).
"""

from __future__ import annotations

PROTECTED_PATH_MARKERS: frozenset[str] = frozenset({
    "VITALTWIN_CONSTITUTION",
    "IMPRESSUM",
    "impressum",
    "DATENSCHUTZ",
    "datenschutz",
    "AGB",
    "agb",
    "WIDERRUFSRECHT",
    "widerrufsrecht",
    "PRIVACY",
    "SECURITY_POLICY",
    "PRICING",
    "PREISE",
    "preise",
    "TERMS",
    "CONTRACT",
    "BRAND",
})


def is_protected(document_path: str) -> bool:
    upper = (document_path or "").upper()
    return any(marker.upper() in upper for marker in PROTECTED_PATH_MARKERS)


def assert_not_protected_for_auto_update(document_path: str) -> None:
    if is_protected(document_path):
        raise PermissionError(
            f"'{document_path}' ist ein geschütztes Dokument — es darf nicht automatisch inhaltlich "
            "verändert werden. Nutze stattdessen einen Change Proposal (Freigabe über das Smart "
            "Approval Center erforderlich)."
        )
