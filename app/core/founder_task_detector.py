"""AI Founder Task Manager — detection engine (VitalTwin Release F3,
Founder Operating System, Module 3).

**No LLM call happens in this module.** "Automatische Aufgabenerkennung"
means: a fixed set of deterministic rules over real, currently-queryable
data (broken affiliate links, missing Stripe/OpenAI configuration, open
support feedback, a spike in failed logins, …). Each rule is fully
auditable — see the `reason`/`data_used`/`impact_if_ignored` fields it
writes, which are template strings built from the actual numbers found,
never free-text generation.

**Only rules backed by a real, already-existing data source are
implemented.** The spec names 16 task-source areas (Affiliate, Premium,
Stripe, Blog, KI, Server, API, Support, SEO, Dokumentation, Tests,
Releases, Backups, Performance, Sicherheit, Analytics) — every one of
them is a valid value of `Source` (so the API/UI can filter/group by all
16 without a future migration), but only the ones below actually run a
detection rule right now, because only those have a real signal in this
codebase:

- **Affiliate**: defekte Links, neue Produkte heute, Produkte zur Freigabe
- **Premium**: Stripe nicht konfiguriert
- **KI**: OpenAI nicht konfiguriert
- **Support**: neues Feedback ohne Reaktion
- **Sicherheit**: ungewöhnlich viele fehlgeschlagene Logins

Blog/Server/API/SEO/Dokumentation/Tests/Releases/Backups/Performance/
Analytics have **no** detection rule yet — there is no CI/CD status, no
server/APM monitoring, no SEO crawler, no doc-freshness check, no test
runner hook, no release tracker, no backup monitor, no cost/performance
telemetry anywhere in this codebase. Adding a fake rule for any of these
would violate the "keine Fake-Daten" mandate; see
`frontend/docs/AI_FOUNDER_TASK_MANAGER.md` for the full breakdown.

**Idempotent, spam-free by design.** Every rule writes to at most one row,
identified by a stable `dedupe_key`. Re-running the scan (which happens on
every `GET /api/admin/founder/tasks` — there is no background job) either:

- creates the task if the condition is newly true and no open task exists,
- refreshes the numbers on the existing open task if still true,
- leaves an already `erledigt`/`archiviert`/ignored task alone (never
  reopens something the founder already dealt with),
- or **auto-resolves** an open, auto-detected task if the underlying
  condition has become false since the last scan — this is what powers
  the honest "Automatisch gelöst" counter in the CEO view.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Callable, Literal

from .integrations import get_ai_providers, get_payment_providers
from .concurrency import run_parallel
from .supabase import supabase

TASK_TABLE = "vt_founder_tasks"
AFFILIATE_PRODUCT_TABLE = "vt_affiliate_products"
FEEDBACK_TABLE = "vt_user_feedback"
LOGIN_EVENT_TABLE = "vt_login_events"

Priority = Literal["kritisch", "hoch", "mittel", "niedrig"]
Status = Literal["neu", "in_bearbeitung", "warten", "erledigt", "archiviert"]
Source = Literal[
    "affiliate", "premium", "stripe", "blog", "ki", "server", "api", "support",
    "seo", "dokumentation", "tests", "releases", "backups", "performance",
    "sicherheit", "analytics",
]
Category = Literal[
    "business", "affiliate", "premium", "marketing", "seo", "blog",
    "technik", "backend", "frontend", "mobile", "android", "ios",
    "ki", "twin", "cgm", "nutrition", "health", "support", "legal", "datenschutz",
]

FAILED_LOGIN_SPIKE_THRESHOLD = 5
MANY_PENDING_APPROVAL_THRESHOLD = 5


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _upsert_or_resolve(*, dedupe_key: str, condition: bool, build_task: Callable[[], dict]) -> None:
    """Core idempotency rule shared by every detector: one row per
    `dedupe_key`, never duplicated, never silently reopened, and
    auto-resolved the moment the condition clears."""
    try:
        existing_rows = (
            supabase.table(TASK_TABLE).select("*").eq("dedupe_key", dedupe_key).limit(1).execute().data or []
        )
    except Exception:
        return
    existing = existing_rows[0] if existing_rows else None

    if not condition:
        if existing and existing.get("status") not in ("erledigt", "archiviert") and existing.get("auto_detected"):
            try:
                supabase.table(TASK_TABLE).update(
                    {
                        "status": "erledigt",
                        "auto_resolved": True,
                        "resolved_at": _now().isoformat(),
                        "updated_at": _now().isoformat(),
                    }
                ).eq("dedupe_key", dedupe_key).execute()
            except Exception:
                pass
        return

    payload = build_task()
    if existing is None:
        payload.update({"dedupe_key": dedupe_key, "status": "neu", "auto_detected": True, "auto_resolved": False})
        try:
            supabase.table(TASK_TABLE).insert(payload).execute()
        except Exception:
            pass
        return

    if existing.get("status") in ("erledigt", "archiviert") or existing.get("ignored"):
        # Founder already dealt with this occurrence — do not reopen or spam.
        return

    payload["updated_at"] = _now().isoformat()
    try:
        supabase.table(TASK_TABLE).update(payload).eq("dedupe_key", dedupe_key).execute()
    except Exception:
        pass


def _detect_affiliate_broken_links() -> None:
    try:
        broken = (
            supabase.table(AFFILIATE_PRODUCT_TABLE).select("id,title").eq("link_status", "broken").execute().data or []
        )
    except Exception:
        broken = []
    count = len(broken)

    def _build():
        return {
            "title": f"{count} Affiliate-Link(s) sind defekt",
            "category": "affiliate",
            "source": "affiliate",
            "priority": "kritisch" if count >= 3 else "hoch",
            "reason": "Automatischer Link-Check hat einen oder mehrere Affiliate-Links als nicht erreichbar markiert.",
            "data_used": f"{count} Produkt(e) mit link_status='broken' in vt_affiliate_products: "
            + ", ".join(p.get("title", "—") for p in broken[:5]),
            "impact_if_ignored": "Nutzer klicken auf einen toten Link, Vertrauen sinkt, keine Provision wird erzielt.",
            "suggested_action": "Link erneut prüfen",
            "suggested_action_available": True,
        }

    _upsert_or_resolve(dedupe_key="affiliate_broken_links", condition=count > 0, build_task=_build)


def _detect_affiliate_new_products_today() -> None:
    today_start = date.today().isoformat()
    try:
        rows = (
            supabase.table(AFFILIATE_PRODUCT_TABLE)
            .select("id,title")
            .gte("created_at", today_start)
            .execute()
            .data
            or []
        )
    except Exception:
        rows = []
    count = len(rows)

    def _build():
        return {
            "title": f"{count} neue Affiliate-Produkt(e) gefunden",
            "category": "affiliate",
            "source": "affiliate",
            "priority": "niedrig",
            "reason": "Neue Produkte wurden heute im Affiliate Center angelegt (created_at heute).",
            "data_used": f"{count} Produkt(e) mit created_at ab {today_start}: " + ", ".join(p.get("title", "—") for p in rows[:5]),
            "impact_if_ignored": "Neue Produkte bleiben im Status 'Entwurf'/'In Prüfung' und werden der KI nie zur Empfehlung freigegeben.",
            "suggested_action": None,
            "suggested_action_available": False,
        }

    # Date-scoped dedupe key: a new task is allowed each day, not reopened forever.
    _upsert_or_resolve(dedupe_key=f"affiliate_new_products_{today_start}", condition=count > 0, build_task=_build)


def _detect_affiliate_pending_approval() -> None:
    try:
        rows = (
            supabase.table(AFFILIATE_PRODUCT_TABLE).select("id,title").eq("status", "in_review").execute().data or []
        )
    except Exception:
        rows = []
    count = len(rows)

    def _build():
        return {
            "title": f"{count} Affiliate-Produkt(e) warten auf Freigabe",
            "category": "affiliate",
            "source": "affiliate",
            "priority": "hoch" if count >= MANY_PENDING_APPROVAL_THRESHOLD else "mittel",
            "reason": "Produkte im Status 'In Prüfung' dürfen der KI erst nach Freigabe empfohlen werden.",
            "data_used": f"{count} Produkt(e) mit status='in_review': " + ", ".join(p.get("title", "—") for p in rows[:5]),
            "impact_if_ignored": "Potenziell gute Affiliate-Produkte werden nie empfohlen, entgangene Einnahmen.",
            "suggested_action": None,
            "suggested_action_available": False,
        }

    _upsert_or_resolve(dedupe_key="affiliate_pending_approval", condition=count > 0, build_task=_build)


def _detect_stripe_not_configured() -> None:
    providers = {p.id: p for p in get_payment_providers()}
    stripe = providers.get("stripe")
    not_configured = stripe is not None and stripe.status == "not_configured"

    def _build():
        return {
            "title": "Stripe ist nicht konfiguriert",
            "category": "premium",
            "source": "premium",
            "priority": "kritisch",
            "reason": "STRIPE_SECRET_KEY ist nicht gesetzt — Premium-Abonnements können nicht verkauft werden.",
            "data_used": "core/integrations.py::get_payment_providers() → stripe.status == 'not_configured'.",
            "impact_if_ignored": "Keine Premium-Umsätze möglich, Checkout schlägt für jeden Nutzer fehl.",
            "suggested_action": None,
            "suggested_action_available": False,
        }

    _upsert_or_resolve(dedupe_key="premium_stripe_not_configured", condition=not_configured, build_task=_build)


def _detect_openai_not_configured() -> None:
    providers = {p.id: p for p in get_ai_providers()}
    openai = providers.get("openai")
    not_configured = openai is not None and openai.status == "not_configured"

    def _build():
        return {
            "title": "OpenAI ist nicht konfiguriert",
            "category": "ki",
            "source": "ki",
            "priority": "kritisch",
            "reason": "OPENAI_API_KEY ist nicht gesetzt — der Twin kann keine KI-Antworten generieren.",
            "data_used": "core/integrations.py::get_ai_providers() → openai.status == 'not_configured'.",
            "impact_if_ignored": "Chat-/Twin-Funktion liefert Nutzern Fehler statt Antworten.",
            "suggested_action": None,
            "suggested_action_available": False,
        }

    _upsert_or_resolve(dedupe_key="ki_openai_not_configured", condition=not_configured, build_task=_build)


def _detect_open_support_feedback() -> None:
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    try:
        rows = (
            supabase.table(FEEDBACK_TABLE).select("id,message").gte("created_at", yesterday).execute().data or []
        )
    except Exception:
        rows = []
    count = len(rows)

    def _build():
        return {
            "title": f"{count} neue Support-Rückmeldung(en) seit gestern",
            "category": "support",
            "source": "support",
            "priority": "mittel",
            "reason": "Neues Feedback in vt_user_feedback seit gestern — es gibt kein Ticket-Status-Feld, daher zeitbasiert statt nach 'offen/geschlossen'.",
            "data_used": f"{count} Eintrag/Einträge in vt_user_feedback mit created_at >= {yesterday}.",
            "impact_if_ignored": "Nutzeranliegen bleiben unbeantwortet, Zufriedenheit sinkt.",
            "suggested_action": None,
            "suggested_action_available": False,
        }

    _upsert_or_resolve(dedupe_key="support_open_feedback", condition=count > 0, build_task=_build)


def _detect_failed_login_spike() -> None:
    day_ago = (_now() - timedelta(hours=24)).isoformat()
    try:
        rows = (
            supabase.table(LOGIN_EVENT_TABLE)
            .select("email")
            .eq("success", False)
            .gte("created_at", day_ago)
            .execute()
            .data
            or []
        )
    except Exception:
        rows = []
    count = len(rows)

    def _build():
        return {
            "title": f"{count} fehlgeschlagene Logins in 24 Stunden",
            "category": "datenschutz",
            "source": "sicherheit",
            "priority": "hoch",
            "reason": f"Mehr als {FAILED_LOGIN_SPIKE_THRESHOLD} fehlgeschlagene Login-Versuche innerhalb von 24 Stunden — möglicher Brute-Force-Versuch.",
            "data_used": f"{count} Zeilen in vt_login_events mit success=false und created_at >= {day_ago}.",
            "impact_if_ignored": "Ein Angreifer könnte unbemerkt weitere Zugriffsversuche unternehmen.",
            "suggested_action": None,
            "suggested_action_available": False,
        }

    _upsert_or_resolve(dedupe_key="security_failed_login_spike", condition=count > FAILED_LOGIN_SPIKE_THRESHOLD, build_task=_build)


def run_detection() -> None:
    """Runs every implemented detection rule once. Called synchronously at
    the start of `GET /api/admin/founder/tasks` — no scheduler, no queue.

    Each detector writes to its own fixed `dedupe_key` (one aggregated
    task per rule, not per row), so running all 7 concurrently instead of
    one after another is safe — no two detectors ever touch the same
    key."""
    run_parallel(
        _detect_affiliate_broken_links,
        _detect_affiliate_new_products_today,
        _detect_affiliate_pending_approval,
        _detect_stripe_not_configured,
        _detect_openai_not_configured,
        _detect_open_support_feedback,
        _detect_failed_login_spike,
    )
