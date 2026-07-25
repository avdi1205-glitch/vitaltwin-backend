"""Auto Documentation — Read-Only Source Scanner (VitalTwin Enterprise,
Founder Operating System, Submodule I).

**Safe, read-only, allowlisted.** This module ONLY ever opens text files
under a hardcoded allowlist of directories inside the *backend* package
(`ALLOWED_SCAN_ROOTS`) — never arbitrary paths, never outside this
repository, never executes or imports scanned files, never follows
symlinks outside the allowlist. No `eval`/`exec`/dynamic import is used
anywhere in this file.

**Critical architectural honesty (see docs/AUTO_DOCUMENTATION.md):** the
backend and frontend are **separate git repositories, deployed
separately** (Railway vs. Vercel) — the running backend process has NO
filesystem access to the frontend repository at all, in production. This
scanner can therefore only verify **backend-side** artifacts (API routes,
Supabase table names from migration SQL, core service modules, test
files). Frontend routes/components/pages and the `frontend/docs/*.md`
files themselves are **not verifiable from this process** — the
Documentation Registry marks such entries `verified=False` with an
explicit note, rather than pretending to have checked them.

No secrets are ever read or returned: `.env`/`.env.example` are not in
the allowlist, and every returned dict only contains file names, route
strings, table names, and docstring-derived text — never full file
contents.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent

ALLOWED_SCAN_ROOTS: tuple[Path, ...] = (
    BACKEND_ROOT / "app" / "routers",
    BACKEND_ROOT / "app" / "core",
    BACKEND_ROOT / "migrations",
    BACKEND_ROOT / "tests",
)

_ROUTE_PATTERN = re.compile(r'@router\.(get|post|put|patch|delete)\(\s*["\']([^"\']+)["\']')
_PERMISSION_PATTERN = re.compile(r'require_admin_permission\([^,]+,\s*["\']([a-z_]+)["\']')
_TABLE_PATTERN = re.compile(r'create table if not exists public\.(\w+)', re.IGNORECASE)
_TEST_FUNC_PATTERN = re.compile(r'^\s*def (test_\w+)', re.MULTILINE)
_DOCSTRING_PATTERN = re.compile(r'"""(.*?)"""', re.DOTALL)


def _is_allowed(path: Path) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        return False
    return any(str(resolved).startswith(str(root)) for root in ALLOWED_SCAN_ROOTS)


def _read_text(path: Path) -> str | None:
    if not _is_allowed(path):
        return None
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None


def _first_docstring_line(text: str) -> str | None:
    match = _DOCSTRING_PATTERN.search(text)
    if not match:
        return None
    lines = [line.strip() for line in match.group(1).strip().splitlines() if line.strip()]
    return lines[0] if lines else None


def scan_api_routes() -> list[dict]:
    """Real, live inventory of every `@router.<method>(...)` decorator in
    `app/routers/*.py` — the actual source of truth for "does this API
    exist", never a manually maintained list that could drift."""
    routers_dir = BACKEND_ROOT / "app" / "routers"
    routes: list[dict] = []
    if not routers_dir.is_dir():
        return routes
    for file in sorted(routers_dir.glob("*.py")):
        text = _read_text(file)
        if text is None:
            continue
        for match in _ROUTE_PATTERN.finditer(text):
            method, path = match.groups()
            # Best-effort: look at the ~200 chars after the decorator for a
            # `require_admin_permission(authorization, "...")` call to
            # report the permission this route enforces (purely textual,
            # never executed).
            window = text[match.end(): match.end() + 400]
            permission_match = _PERMISSION_PATTERN.search(window)
            routes.append({
                "method": method.upper(),
                "path": path,
                "router_file": file.name,
                "permission": permission_match.group(1) if permission_match else None,
            })
    return routes


def scan_data_models() -> list[dict]:
    """Real table names extracted from `migrations/*.sql` — the actual
    Supabase schema this backend expects, not a hand-maintained list."""
    migrations_dir = BACKEND_ROOT / "migrations"
    tables: dict[str, dict] = {}
    if not migrations_dir.is_dir():
        return []
    for file in sorted(migrations_dir.glob("*.sql")):
        text = _read_text(file)
        if text is None:
            continue
        for match in _TABLE_PATTERN.finditer(text):
            table_name = match.group(1)
            tables.setdefault(table_name, {"table": table_name, "migration_file": file.name})
    return list(tables.values())


def scan_migrations() -> list[dict]:
    """Real list of migration files — name + whether it is the highest-
    numbered (latest) migration."""
    migrations_dir = BACKEND_ROOT / "migrations"
    if not migrations_dir.is_dir():
        return []
    files = sorted(f.name for f in migrations_dir.glob("*.sql"))
    return [{"file": name, "is_latest": name == files[-1] if files else False} for name in files]


def scan_core_services() -> list[dict]:
    """Real list of `app/core/*.py` modules with their first docstring
    line (never full content) as a one-line purpose summary."""
    core_dir = BACKEND_ROOT / "app" / "core"
    services: list[dict] = []
    if not core_dir.is_dir():
        return services
    for file in sorted(core_dir.glob("*.py")):
        if file.name == "__init__.py":
            continue
        text = _read_text(file)
        services.append({"module": file.stem, "purpose": _first_docstring_line(text or "") or "Kein Docstring gefunden."})
    return services


def scan_test_files() -> list[dict]:
    """Real per-file test function counts from `tests/*.py`."""
    tests_dir = BACKEND_ROOT / "tests"
    results: list[dict] = []
    if not tests_dir.is_dir():
        return results
    for file in sorted(tests_dir.glob("test_*.py")):
        text = _read_text(file)
        count = len(_TEST_FUNC_PATTERN.findall(text or ""))
        results.append({"file": file.name, "test_count": count})
    return results


def compute_backend_source_hash() -> str:
    """Cheap, real change-detection hash over routers/core/migrations file
    names + sizes + mtimes — used by stale detection to know whether the
    backend source tree changed since a document was last generated.
    Deliberately does NOT hash full file contents (avoids reading large
    trees on every dashboard load)."""
    fingerprint_parts: list[str] = []
    for root in (BACKEND_ROOT / "app" / "routers", BACKEND_ROOT / "app" / "core", BACKEND_ROOT / "migrations"):
        if not root.is_dir():
            continue
        for file in sorted(root.glob("*")):
            if not file.is_file():
                continue
            try:
                stat = file.stat()
            except OSError:
                continue
            fingerprint_parts.append(f"{file.name}:{stat.st_size}:{int(stat.st_mtime)}")
    return hashlib.sha256("|".join(fingerprint_parts).encode("utf-8")).hexdigest()
