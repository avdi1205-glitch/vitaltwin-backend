"""Shared helper for running independent, blocking Supabase lookups
concurrently instead of one after another.

Founder Operating System aggregation endpoints (dashboards, overviews,
scores) each make several *independent* supabase-py calls — real blocking
HTTP round-trips, since supabase-py is a synchronous client — that don't
depend on each other's results. Running them one-by-one means the total
latency is the SUM of every call; running them concurrently in a small
thread pool means it's roughly the latency of the SLOWEST single call.

This is a plain `ThreadPoolExecutor` helper (not asyncio) so it can be
used directly inside synchronous core/*.py aggregation functions without
turning their whole call chain (and every test that calls them directly)
into async code. The outer FastAPI route handler is still `async def` and
should offload the whole synchronous aggregation to a worker thread via
`await asyncio.to_thread(...)` so the event loop stays free to start
handling other concurrent requests (e.g. the several endpoints a single
admin tab fetches in parallel via `Promise.all` on the frontend).
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Callable, TypeVar

T = TypeVar("T")


def run_parallel(*funcs: Callable[[], T]) -> list[T]:
    """Runs each zero-argument callable concurrently in a thread pool and
    returns their results in the same order they were passed in. Each
    callable is expected to handle its own exceptions (matching this
    codebase's "honest `None`/empty-list on failure" convention) — a
    callable that raises will propagate that exception to the caller,
    same as calling it directly would."""
    if not funcs:
        return []
    with ThreadPoolExecutor(max_workers=len(funcs)) as executor:
        futures = [executor.submit(fn) for fn in funcs]
        return [future.result() for future in futures]
