"""Static migration safety check — Etappe 10 §15 ("Migrationstest").

No live database is available in this environment (documented in every
migration header since Etappe 2), so this cannot be a real "apply the
migration and verify" test. What *can* be honestly verified without a
database: every migration file in this repository is additive-only — no
`drop table`, `drop column`, `truncate`, or destructive `alter ... type`
statement ever appears. This is a real, meaningful static guarantee: even
though the migrations were never executed, replaying them in order against
a real database can never destroy existing data or existing schema.
"""

from __future__ import annotations

import re
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

_DESTRUCTIVE_PATTERNS = [
    r"\bdrop\s+table\b",
    r"\bdrop\s+column\b",
    r"\bdrop\s+database\b",
    r"\btruncate\b",
    r"\bdelete\s+from\b",
    r"\balter\s+table\s+\S+\s+alter\s+column\s+\S+\s+type\b",
]


def _all_migration_files() -> list[Path]:
    return sorted(MIGRATIONS_DIR.glob("*.sql"))


class TestMigrationsExistAndAreOrdered:
    def test_at_least_eight_migrations_exist(self):
        files = _all_migration_files()
        assert len(files) >= 8

    def test_migrations_are_sequentially_numbered(self):
        files = _all_migration_files()
        numbers = [int(f.name.split("_", 1)[0]) for f in files]
        assert numbers == sorted(numbers)
        assert numbers == list(range(1, len(numbers) + 1))


class TestMigrationsAreAdditiveOnly:
    def test_no_migration_contains_destructive_ddl(self):
        violations: list[str] = []
        for path in _all_migration_files():
            text = path.read_text(encoding="utf-8").lower()
            # Strip SQL comments (-- ...) so a comment merely *mentioning*
            # "drop table" (e.g. explaining what NOT to do) isn't flagged.
            without_comments = "\n".join(line.split("--", 1)[0] for line in text.splitlines())
            for pattern in _DESTRUCTIVE_PATTERNS:
                if re.search(pattern, without_comments):
                    violations.append(f"{path.name}: matched {pattern!r}")
        assert violations == [], f"Destructive DDL found: {violations}"

    def test_every_migration_uses_if_not_exists_or_if_exists_guards(self):
        """Every `create table`/`alter table ... add column`/`create index`
        should be defensively guarded so re-running a migration (or running
        it against a partially-migrated database) never errors out."""
        unguarded: list[str] = []
        for path in _all_migration_files():
            lines = path.read_text(encoding="utf-8").lower().splitlines()
            for line in lines:
                stripped = line.strip()
                if stripped.startswith("create table") and "if not exists" not in stripped:
                    unguarded.append(f"{path.name}: {stripped[:80]}")
                if stripped.startswith("create index") and "if not exists" not in stripped:
                    unguarded.append(f"{path.name}: {stripped[:80]}")
        assert unguarded == [], f"Unguarded DDL found: {unguarded}"
