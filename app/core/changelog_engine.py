"""Auto Documentation — Changelog Engine (VitalTwin Enterprise, Founder
Operating System, Submodule I).

**Real sources only**: git commit history (via one fixed, read-only `git
log` command — no shell, no string interpolation, no user input reaches
the subprocess call), the live migration file list (Database category),
and the live API route scan (API category). If `git` is unavailable in
the deployment environment (e.g. a filesystem without `.git`), this is
detected and reported honestly — the changelog falls back to a
structured "current state" snapshot instead of a true commit diff, never
a fabricated entry list.
"""

from __future__ import annotations

import shutil
import subprocess

from . import documentation_scanner as scanner

GIT_LOG_LIMIT = 30

CHANGELOG_CATEGORIES = (
    "Added", "Changed", "Fixed", "Deprecated", "Removed", "Security",
    "Performance", "Documentation", "Database", "API", "UI", "Internal",
)


def _git_available() -> bool:
    return shutil.which("git") is not None and (scanner.BACKEND_ROOT / ".git").exists()


def _read_git_log() -> list[str] | None:
    if not _git_available():
        return None
    try:
        result = subprocess.run(
            ["git", "log", f"-{GIT_LOG_LIMIT}", "--pretty=format:%s"],
            cwd=scanner.BACKEND_ROOT, capture_output=True, text=True, timeout=5, check=False,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if result.returncode != 0:
        return None
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _categorize_commit_message(message: str) -> str:
    lower = message.lower()
    if lower.startswith("feat"):
        return "Added"
    if lower.startswith("fix"):
        return "Fixed"
    if lower.startswith("docs"):
        return "Documentation"
    if lower.startswith("perf"):
        return "Performance"
    if lower.startswith("security") or "security" in lower:
        return "Security"
    if lower.startswith("remove") or lower.startswith("delete"):
        return "Removed"
    if lower.startswith("chore") or lower.startswith("refactor"):
        return "Internal"
    return "Changed"


def generate_changelog_draft() -> dict:
    commits = _read_git_log()
    entries: dict[str, list[str]] = {category: [] for category in CHANGELOG_CATEGORIES}

    if commits is not None:
        for message in commits:
            entries[_categorize_commit_message(message)].append(message)
        source_note = f"Aus den letzten {len(commits)} Git-Commit-Nachrichten kategorisiert (Präfix-Heuristik: feat/fix/docs/perf/chore)."
        git_available = True
    else:
        source_note = "Kein Git-Verlauf verfügbar in dieser Umgebung — zeigt stattdessen den aktuellen Datenbank-/API-Stand."
        git_available = False
        entries["Database"] = [f"Tabelle: {t['table']} ({t['migration_file']})" for t in scanner.scan_data_models()]
        entries["API"] = [f"{r['method']} {r['path']} ({r['router_file']})" for r in scanner.scan_api_routes()]

    return {
        "git_available": git_available,
        "source_note": source_note,
        "categories": {k: v for k, v in entries.items() if v},
        "empty_categories": [k for k, v in entries.items() if not v],
    }
