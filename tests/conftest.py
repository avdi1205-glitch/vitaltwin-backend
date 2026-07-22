"""Pytest configuration for the backend test suite.

Ensures `SUPABASE_URL`/`SUPABASE_KEY` placeholders exist before `app.core.supabase`
is imported anywhere (module-level `create_client(...)` call), so tests that
only exercise pure validation/auth logic don't fail just because no real
Supabase project is configured in this environment. Tests that need an actual
database connection are marked and skipped — see `tests/README.md`.
"""

import os

os.environ.setdefault("SUPABASE_URL", "https://placeholder.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "placeholder-key-for-tests")
os.environ.setdefault("JWT_SECRET_KEY", "test-only-secret-do-not-use-in-production")
