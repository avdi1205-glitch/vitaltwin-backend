"""Auto Documentation — Release Notes Engine (VitalTwin Enterprise,
Founder Operating System, Submodule I).

Builds **two** views from the exact same underlying real data
(`changelog_engine.py`) — never two independently-sourced truths:

- **Internal Release Notes**: every category, technical wording, includes
  Database/API/Security/Internal sections.
- **User-Facing Release Notes**: only the categories a customer cares
  about (Added/Changed/Fixed/Removed), simplified wording, no internal
  security/database/API details ever included — this is a pure
  presentation filter over the same source, not a second data source.

Both always require Approval Center sign-off before public release (per
spec, "öffentliche Release Notes" needs founder/super_admin approval) —
this module only ever prepares a **draft**.
"""

from __future__ import annotations

from . import changelog_engine

INTERNAL_CATEGORIES = changelog_engine.CHANGELOG_CATEGORIES
USER_FACING_CATEGORIES = ("Added", "Changed", "Fixed", "Removed")

USER_FACING_LABELS = {
    "Added": "Neu", "Changed": "Verbessert", "Fixed": "Behoben", "Removed": "Entfernt",
}


def generate_internal_release_notes() -> dict:
    draft = changelog_engine.generate_changelog_draft()
    return {
        "audience": "intern",
        "git_available": draft["git_available"],
        "source_note": draft["source_note"],
        "sections": draft["categories"],
        "requires_approval": False,
        "approval_note": "Interne Release Notes benötigen keine Freigabe — nur die Veröffentlichung nach außen.",
    }


def generate_user_release_notes() -> dict:
    draft = changelog_engine.generate_changelog_draft()
    sections = {
        USER_FACING_LABELS[category]: entries
        for category, entries in draft["categories"].items()
        if category in USER_FACING_CATEGORIES and entries
    }
    return {
        "audience": "nutzer",
        "git_available": draft["git_available"],
        "sections": sections,
        "requires_approval": True,
        "approval_note": "Öffentliche Release Notes benötigen immer eine Freigabe im Smart Approval Center vor Veröffentlichung.",
        "known_limitations_note": "Enthält keine internen Sicherheits-, Datenbank- oder API-Details.",
    }
