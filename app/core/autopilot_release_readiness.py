"""Founder Autopilot — Release Readiness (VitalTwin Enterprise, Founder
Operating System, Submodule J).

**Conservative by construction.** This backend cannot safely verify
TypeScript/Lint/Test/Build success at request time (that would require
either executing arbitrary build tooling — a security risk this project
explicitly forbids — or a CI webhook receiver that does not exist yet).
Those checks are therefore always `verifiable: false` and **count against
readiness** (never assumed to have passed) — the overall verdict can
never be `"bereit"` while any real, checkable blocker exists, and is
capped at `"bereit mit offenen Punkten"` at best given the unverifiable
checks, unless the founder has manually confirmed them elsewhere.

Founder Autopilot never publishes a release itself — this endpoint is
read-only information, nothing here triggers a deploy.
"""

from __future__ import annotations

from . import documentation_score
from .supabase import supabase

Verdict = str  # "bereit" | "bereit_mit_offenen_punkten" | "nicht_bereit"


def _check(*, name: str, verifiable: bool, passed: bool | None, detail: str) -> dict:
    return {"name": name, "verifiable": verifiable, "passed": passed, "detail": detail}


def compute_release_readiness() -> dict:
    checks = []

    for name in ("TypeScript", "Lint", "Tests", "Build", "Mobile-Ansicht", "Security Checks"):
        checks.append(_check(
            name=name, verifiable=False, passed=None,
            detail="Nicht automatisch prüfbar in diesem Backend-Prozess (kein CI/CD-Webhook angebunden) — letzter manueller Lauf durch den Gründer maßgeblich.",
        ))

    try:
        critical_bugs = len([
            t for t in (supabase.table("vt_founder_tasks").select("priority,status").execute().data or [])
            if t.get("priority") == "kritisch" and t.get("status") in ("neu", "in_bearbeitung", "warten")
        ])
        checks.append(_check(name="Offene kritische Bugs", verifiable=True, passed=critical_bugs == 0, detail=f"{critical_bugs} offene kritische Aufgaben."))
    except Exception:
        checks.append(_check(name="Offene kritische Bugs", verifiable=False, passed=None, detail="vt_founder_tasks nicht erreichbar."))

    try:
        critical_approvals = len([
            a for a in (supabase.table("vt_founder_approvals").select("priority,status").execute().data or [])
            if a.get("priority") == "kritisch" and a.get("status") in ("neu", "ki_geprueft", "zur_pruefung")
        ])
        checks.append(_check(name="Offene kritische Freigaben", verifiable=True, passed=critical_approvals == 0, detail=f"{critical_approvals} offene kritische Freigaben."))
    except Exception:
        checks.append(_check(name="Offene kritische Freigaben", verifiable=False, passed=None, detail="vt_founder_approvals nicht erreichbar."))

    try:
        doc_score = documentation_score.compute_documentation_score()
        coverage = doc_score.get("overall_percentage")
        checks.append(_check(name="Dokumentation", verifiable=coverage is not None, passed=(coverage or 0) >= 70 if coverage is not None else None,
                              detail=f"Dokumentationsabdeckung: {coverage}%." if coverage is not None else "Keine Abdeckungsdaten vorhanden."))
    except Exception:
        checks.append(_check(name="Dokumentation", verifiable=False, passed=None, detail="Auto Documentation nicht verfügbar."))

    checks.append(_check(name="Migrationen ausgeführt", verifiable=False, passed=None,
                          detail="Nicht automatisch verifizierbar, ob alle Migrationsdateien bereits in Supabase ausgeführt wurden."))
    checks.append(_check(name="Rollback-Möglichkeit", verifiable=False, passed=None,
                          detail="Kein automatisierter Rollback-Nachweis — siehe Dokumentation je Migration."))

    verifiable_checks = [c for c in checks if c["verifiable"]]
    failed_verifiable = [c for c in verifiable_checks if c["passed"] is False]
    unverifiable_count = len([c for c in checks if not c["verifiable"]])

    if failed_verifiable:
        verdict: Verdict = "nicht_bereit"
    elif unverifiable_count > 0:
        verdict = "bereit_mit_offenen_punkten"
    else:
        verdict = "bereit"

    return {
        "verdict": verdict,
        "checks": checks,
        "note": "Founder Autopilot ver\u00f6ffentlicht niemals selbst einen Release — diese Ansicht ist rein informativ.",
    }
