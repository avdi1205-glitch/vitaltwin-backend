import os

from supabase import create_client

supabase_url = os.getenv("SUPABASE_URL", "")
supabase_key = os.getenv("SUPABASE_KEY", "")

supabase = create_client(supabase_url, supabase_key)

# Server-only privileged client for the small set of operations that must
# never be reachable via the plain anon role (e.g. PII tables with RLS
# enabled and zero anon policies, like `vt_beta_applications`). Deliberately
# a SEPARATE client, not a replacement — every other existing call site keeps
# using the regular `supabase` (anon) client above, unchanged.
# `SUPABASE_SERVICE_ROLE_KEY` is never fabricated: if unset, `supabase_admin`
# stays `None` and callers must fail closed (never silently fall back to the
# anon client for a privileged operation).
_supabase_service_role_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
supabase_admin = create_client(supabase_url, _supabase_service_role_key) if _supabase_service_role_key else None
