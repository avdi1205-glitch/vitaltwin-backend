"""Affiliate Intelligence — API (VitalTwin Enterprise, Founder Operating
System, Submodule F).

Mounted at `/api/admin/founder` in `app/main.py` (own file, same prefix as
every other Founder-OS router). Reuses `view_founder_os`/`manage_founder_os`
— no new fragmented permission pair.

**No parallel affiliate system.** This router is a read-mostly
intelligence layer over the *existing* Affiliate Platform
(`routers/affiliate_admin.py`, `core/affiliate_engine.py`,
`core/affiliate_import_export.py`, `core/affiliate_link_checker.py`) plus
the existing Founder-OS modules (`core/founder_approval_detector.py`,
`core/founder_business_metrics.py`). It never re-implements product CRUD,
partner CRUD, blacklist, campaigns, or A/B tests — those remain exactly
where they already are.

**No LLM call except one, optional, on-demand endpoint**
(`ai_review_product`) — every other function here (dashboard, product
health, duplicates, approval assistant, ranking, simulator, automation
score) is deterministic and rule-based, exactly like every other
Founder-OS detector.
"""

from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, field_validator

from ..core import affiliate_dedup as dedup
from ..core import affiliate_provider as provider_module
from ..core import affiliate_ranking as ranking
from ..core import founder_approval_detector
from ..core import founder_business_metrics as metrics
from ..core.admin_rbac import require_admin_permission
from ..core.affiliate_intelligence_detector import run_affiliate_intelligence_detection
from ..core.affiliate_product_health import compute_product_health
from ..core.affiliate_review_rules import review_product_rule_based, summarize_approval_assistant
from ..core.audit import record_audit_event
from ..core.concurrency import run_parallel
from ..core.rate_limit import enforce_rate_limit
from ..core.supabase import supabase
from ..services.ai_provider import AIProvider, AIProviderError, OpenAIProvider

router = APIRouter()

PRODUCT_TABLE = "vt_affiliate_products"
PARTNER_TABLE = "vt_affiliate_partners"
CATEGORY_TABLE = "vt_affiliate_categories"
BLACKLIST_TABLE = "vt_affiliate_blacklist"
EVENT_TABLE = "vt_affiliate_events"
DUPLICATE_TABLE = "vt_affiliate_duplicate_candidates"
TASK_TABLE = "vt_founder_tasks"
APPROVAL_TABLE = "vt_founder_approvals"

# Simple, real, keyword-based context mapping for the Recommendation
# Simulator — deterministic, no LLM call needed to pick a category.
SIMULATOR_CONTEXT_KEYWORDS = {
    "schlaf": ["schlaf", "besser schlafen"],
    "bewegung": ["bewegung", "fitness", "sport"],
    "ernährung": ["ernährung", "meal prep", "kochen", "essen"],
    "hydration": ["hydration", "trinken", "wasser"],
    "meditation": ["meditation", "achtsamkeit", "entspannung"],
    "cgm": ["cgm", "blutzucker"],
}

MAX_AI_REVIEWS_PER_DAY = 20


class DuplicateResolutionInput(BaseModel):
    status: str

    @field_validator("status")
    @classmethod
    def _validate_status(cls, value: str) -> str:
        allowed = {"bestaetigtes_duplikat", "getrennt_bestaetigt"}
        if value not in allowed:
            raise ValueError(f"Ungültiger Status. Erlaubt: {', '.join(sorted(allowed))}")
        return value


class BulkSendInput(BaseModel):
    product_ids: list[str]


class SimulateInput(BaseModel):
    context: str


def _get_ai_provider() -> AIProvider:
    return OpenAIProvider()


def _fetch_blacklist_sets() -> dict[str, set[str]]:
    result: dict[str, set[str]] = {"product": set(), "brand": set(), "partner": set(), "category": set()}
    try:
        rows = supabase.table(BLACKLIST_TABLE).select("entry_type,value").execute().data or []
    except Exception:
        return result
    for row in rows:
        entry_type = row.get("entry_type")
        if entry_type in result and row.get("value"):
            result[entry_type].add(row["value"])
    return result


def _is_blacklisted(product: dict, blacklist: dict[str, set[str]], category_name: str | None) -> bool:
    if str(product.get("id")) in blacklist["product"]:
        return True
    if product.get("brand") and product["brand"] in blacklist["brand"]:
        return True
    if product.get("partner_id") and str(product["partner_id"]) in blacklist["partner"]:
        return True
    if category_name and category_name in blacklist["category"]:
        return True
    return False


def _category_name_map() -> dict[str, str]:
    try:
        rows = supabase.table(CATEGORY_TABLE).select("id,name").execute().data or []
    except Exception:
        return {}
    return {str(r["id"]): r["name"] for r in rows}


def _open_duplicate_product_ids() -> set[str]:
    try:
        rows = supabase.table(DUPLICATE_TABLE).select("product_a_id,product_b_id").eq("status", "moegliches_duplikat").execute().data or []
    except Exception:
        return set()
    ids: set[str] = set()
    for row in rows:
        ids.add(str(row["product_a_id"]))
        ids.add(str(row["product_b_id"]))
    return ids


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


@router.get("/affiliate-intelligence/dashboard")
async def affiliate_intelligence_dashboard(authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_founder_os")

    today = date.today()
    today_start = today.isoformat()
    month_start = today.replace(day=1).isoformat()
    soon = (today + timedelta(days=7)).isoformat()

    def _products() -> list[dict]:
        try:
            return supabase.table(PRODUCT_TABLE).select("*").execute().data or []
        except Exception:
            return []

    def _partners() -> list[dict]:
        try:
            return supabase.table(PARTNER_TABLE).select("*").execute().data or []
        except Exception:
            return []

    def _events_today() -> list[dict]:
        try:
            return supabase.table(EVENT_TABLE).select("event_type,revenue,commission").gte("created_at", today_start).execute().data or []
        except Exception:
            return []

    def _events_month() -> list[dict]:
        try:
            return supabase.table(EVENT_TABLE).select("event_type,revenue,commission").gte("created_at", month_start).execute().data or []
        except Exception:
            return []

    def _open_tasks() -> int:
        try:
            return len([t for t in (supabase.table(TASK_TABLE).select("status,category").execute().data or []) if t.get("category") == "affiliate" and t.get("status") in ("neu", "in_bearbeitung", "warten")])
        except Exception:
            return 0

    def _open_approvals() -> int:
        try:
            return len([a for a in (supabase.table(APPROVAL_TABLE).select("status,category").execute().data or []) if a.get("category") in ("affiliate", "business") and a.get("status") in ("neu", "ki_geprueft", "zur_pruefung")])
        except Exception:
            return 0

    # All 9 branches below are independent of each other (the 2 detection
    # runs write to their own tables and aren't read by anything else in
    # this function) — run them concurrently instead of one after another.
    (
        _,
        _,
        products,
        partners,
        providers,
        events_today,
        events_month,
        open_tasks,
        open_approvals,
    ) = run_parallel(
        run_affiliate_intelligence_detection,
        founder_approval_detector.run_detection,  # Keeps existing per-item affiliate approvals fresh — no duplicate logic.
        _products,
        _partners,
        provider_module.get_provider_statuses,
        _events_today,
        _events_month,
        _open_tasks,
        _open_approvals,
    )

    new_products_today = sum(1 for p in products if str(p.get("created_at", "")) >= today_start)
    pending_approval = sum(1 for p in products if p.get("status") == "in_review")
    paused = sum(1 for p in products if p.get("status") == "paused")
    broken_links = sum(1 for p in products if p.get("link_status") == "broken")
    active_products = sum(1 for p in products if p.get("status") in ("approved", "active"))
    expiring_soon = sum(1 for p in products if p.get("end_date") and today_start <= str(p["end_date"]) <= soon)
    checked_products = sum(1 for p in products if p.get("link_last_checked_at"))

    impressions_today = sum(1 for e in events_today if e.get("event_type") == "impression")
    clicks_today = sum(1 for e in events_today if e.get("event_type") == "click")
    conversions_today = sum(1 for e in events_today if e.get("event_type") == "conversion")
    commission_today = sum(float(e.get("commission") or 0) for e in events_today if e.get("event_type") == "conversion")
    commission_month = sum(float(e.get("commission") or 0) for e in events_month if e.get("event_type") == "conversion")
    conversion_rate = round(conversions_today / clicks_today, 3) if clicks_today else None

    return {
        "computed_at": today.isoformat(),
        "active_partner_programs": {"value": sum(1 for p in partners if p.get("status") == "active"), "source": "vt_affiliate_partners"},
        "connected_apis": {"value": sum(1 for p in providers if p.configured and p.kind == "network_api"), "source": "core/affiliate_provider.py"},
        "erroring_apis": {"value": 0, "note": "Keine Netzwerk-API ist verbunden, daher kann keine fehlerhaft sein.", "source": "core/affiliate_provider.py"},
        "last_sync": {"value": None, "note": "Keine automatische, zeitgesteuerte Synchronisation implementiert — nur manueller Import.", "source": "core/affiliate_import_export.py"},
        "new_products_today": {"value": new_products_today, "source": "vt_affiliate_products"},
        "products_checked": {"value": checked_products, "source": "vt_affiliate_products (link_last_checked_at gesetzt)"},
        "pending_approval": {"value": pending_approval, "source": "vt_affiliate_products (status='in_review')"},
        "auto_paused": {"value": paused, "source": "vt_affiliate_products (status='paused')"},
        "broken_links": {"value": broken_links, "source": "vt_affiliate_products (link_status='broken')"},
        "expiring_soon": {"value": expiring_soon, "source": "vt_affiliate_products (end_date <= 7 Tage)"},
        "active_products": {"value": active_products, "source": "vt_affiliate_products (status in approved/active)"},
        "impressions_today": {"value": impressions_today, "source": "vt_affiliate_events"},
        "clicks_today": {"value": clicks_today, "source": "vt_affiliate_events"},
        "conversions_today": {"value": conversions_today, "source": "vt_affiliate_events"},
        "commission_today": {"value": round(commission_today, 2), "source": "vt_affiliate_events"},
        "commission_month": {"value": round(commission_month, 2), "source": "vt_affiliate_events"},
        "conversion_rate": {"value": conversion_rate, "note": None if conversion_rate is not None else "Keine Klicks heute.", "source": "vt_affiliate_events"},
        "open_tasks": {"value": open_tasks, "source": "vt_founder_tasks"},
        "open_approvals": {"value": open_approvals, "source": "vt_founder_approvals"},
    }


@router.get("/affiliate-intelligence/providers")
async def list_providers(authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_founder_os")
    return {"items": [p.__dict__ for p in provider_module.get_provider_statuses()]}


# ---------------------------------------------------------------------------
# Product Health
# ---------------------------------------------------------------------------


@router.get("/affiliate-intelligence/product-health")
async def product_health(authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_founder_os")
    try:
        products = supabase.table(PRODUCT_TABLE).select("*").execute().data or []
    except Exception:
        products = []
    category_names = _category_name_map()
    blacklist = _fetch_blacklist_sets()

    items = []
    for product in products:
        category_name = category_names.get(str(product.get("category_id")))
        blacklisted = _is_blacklisted(product, blacklist, category_name)
        health = compute_product_health(product, blacklisted=blacklisted)
        items.append({"product_id": product["id"], "title": product.get("title"), **health})
    return {"items": items}


# ---------------------------------------------------------------------------
# Dublettenerkennung
# ---------------------------------------------------------------------------


@router.get("/affiliate-intelligence/duplicates")
async def list_duplicates(authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_founder_os")
    try:
        candidates = supabase.table(DUPLICATE_TABLE).select("*").order("created_at", desc=True).execute().data or []
    except Exception:
        candidates = []
    return {"items": candidates}


@router.post("/affiliate-intelligence/duplicates/{candidate_id}/resolve")
async def resolve_duplicate(candidate_id: str, data: DuplicateResolutionInput, authorization: str | None = Header(default=None)):
    admin = require_admin_permission(authorization, "manage_founder_os")
    dedup.resolve_duplicate_candidate(candidate_id, status=data.status, resolved_by=admin.email)
    record_audit_event(user_id=None, email=admin.email, action="update", entity_type="affiliate_duplicate_candidate", entity_id=candidate_id, metadata={"status": data.status})
    return {"message": "Dubletten-Kandidat aktualisiert."}


# ---------------------------------------------------------------------------
# Approval Assistant
# ---------------------------------------------------------------------------


@router.get("/affiliate-intelligence/approval-assistant")
async def approval_assistant(authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_founder_os")
    try:
        products = (
            supabase.table(PRODUCT_TABLE).select("*").execute().data or []
        )
    except Exception:
        products = []
    review_candidates = [p for p in products if p.get("status") in ("in_review", "imported", "normalized", "needs_review")]

    category_names = _category_name_map()
    blacklist = _fetch_blacklist_sets()
    duplicate_ids = _open_duplicate_product_ids()

    reviews = []
    for product in review_candidates:
        category_name = category_names.get(str(product.get("category_id")))
        blacklisted = _is_blacklisted(product, blacklist, category_name)
        has_duplicate = str(product["id"]) in duplicate_ids
        review = review_product_rule_based(product, category_name=category_name, blacklisted=blacklisted, has_duplicate_candidate=has_duplicate)
        reviews.append({"product_id": product["id"], "title": product.get("title"), **review})

    return {"summary": summarize_approval_assistant(reviews), "items": reviews}


@router.post("/affiliate-intelligence/approval-assistant/send-bulk")
async def send_bulk_to_approval(data: BulkSendInput, authorization: str | None = Header(default=None)):
    """Sends the founder-selected 'sammelfreigabe' products into the
    existing Approval Center pipeline (Submodule D) — sets each product's
    status to `in_review` (if not already) so the already-existing
    `founder_approval_detector` picks them up; no parallel approval
    mechanism is created here."""
    admin = require_admin_permission(authorization, "manage_founder_os")
    updated = 0
    for product_id in data.product_ids:
        try:
            supabase.table(PRODUCT_TABLE).update({"status": "in_review"}).eq("id", product_id).execute()
            updated += 1
        except Exception:
            continue
    founder_approval_detector.run_detection()
    record_audit_event(user_id=None, email=admin.email, action="update", entity_type="affiliate_bulk_send", metadata={"count": updated})
    return {"message": "An das Smart Approval Center übergeben.", "updated": updated}


@router.post("/affiliate-intelligence/products/{product_id}/ai-review")
async def ai_review_product(product_id: str, request: Request, authorization: str | None = Header(default=None)):
    """The one, optional, on-demand LLM call in this module — reuses the
    existing `services/ai_provider.py` abstraction, never a new provider
    binding. Never auto-approves anything; returns a deeper explanatory
    text only."""
    admin = require_admin_permission(authorization, "manage_founder_os")
    enforce_rate_limit(request, "affiliate_ai_review", max_requests=MAX_AI_REVIEWS_PER_DAY, window_seconds=86400)
    try:
        rows = supabase.table(PRODUCT_TABLE).select("*").eq("id", product_id).limit(1).execute().data or []
    except Exception:
        rows = []
    if not rows:
        raise HTTPException(status_code=404, detail="Produkt nicht gefunden.")
    product = rows[0]

    system_prompt = (
        "Du pruefst ein Affiliate-Produkt fuer VitalTwin, ein Wellness-Produkt (kein Medizinprodukt). "
        "Sag ehrlich, ob Titel/Beschreibung zur Mission passen, ob problematische Heilversprechen oder "
        "irrefuehrende Gesundheitsaussagen enthalten sind. Du entscheidest NICHT ueber rechtliche Zulaessigkeit "
        "-- das bleibt Aufgabe des Gruenders. Antworte kurz auf Deutsch."
    )
    context_text = f"Titel: {product.get('title')}\nBeschreibung: {product.get('description') or '(keine)'}\nMarke: {product.get('brand') or '(unbekannt)'}"

    provider = _get_ai_provider()
    try:
        explanation = await provider.generate_recommendation_explanation(system_prompt=system_prompt, context_text=context_text)
    except AIProviderError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    try:
        supabase.table(PRODUCT_TABLE).update({"ai_reviewed": True}).eq("id", product_id).execute()
    except Exception:
        pass
    record_audit_event(user_id=None, email=admin.email, action="update", entity_type="affiliate_product", entity_id=product_id, metadata={"ai_reviewed": True})
    return {"explanation": explanation}


# ---------------------------------------------------------------------------
# Recommendation Simulator
# ---------------------------------------------------------------------------


@router.post("/affiliate-intelligence/simulate")
async def simulate_recommendations(data: SimulateInput, authorization: str | None = Header(default=None)):
    """Uses a neutral test context string — never real personal user data
    (per spec). Reuses `core/affiliate_engine.py::get_eligible_products`
    for the actual eligibility rules (no parallel filter logic)."""
    require_admin_permission(authorization, "view_founder_os")

    from ..core.affiliate_engine import get_eligible_products

    context_lower = data.context.strip().lower()
    matched_category_key = None
    for key, keywords in SIMULATOR_CONTEXT_KEYWORDS.items():
        if any(kw in context_lower for kw in keywords):
            matched_category_key = key
            break

    category_names = _category_name_map()
    category_id = None
    if matched_category_key:
        for cid, name in category_names.items():
            if matched_category_key in name.lower():
                category_id = cid
                break

    try:
        all_products = supabase.table(PRODUCT_TABLE).select("*").execute().data or []
    except Exception:
        all_products = []

    eligible = get_eligible_products(category=category_id, limit=10)
    eligible_ids = {str(p["id"]) for p in eligible}

    blacklist = _fetch_blacklist_sets()
    excluded = []
    for product in all_products:
        if str(product["id"]) in eligible_ids:
            continue
        category_name = category_names.get(str(product.get("category_id")))
        reason = "Nicht im gewählten Kontext relevant."
        if product.get("status") not in ("approved", "active"):
            reason = f"Status '{product.get('status')}' ist nicht freigegeben."
        elif product.get("link_status") == "broken":
            reason = "Affiliate-Link ist defekt."
        elif _is_blacklisted(product, blacklist, category_name):
            reason = "Produkt/Marke/Partner/Kategorie ist gesperrt."
        excluded.append({"product_id": product["id"], "title": product.get("title"), "reason": reason})

    ranked = []
    for product in eligible:
        score = ranking.compute_product_score(product, context_category_id=category_id)
        ranked.append({"product_id": product["id"], "title": product.get("title"), "category": category_names.get(str(product.get("category_id"))), "status": product.get("status"), **score, "disclosure": "Partnerempfehlung / Affiliate Link / Werbung"})
    ranked.sort(key=lambda p: p["score"], reverse=True)

    return {"matched_category": category_names.get(category_id) if category_id else None, "recommended": ranked, "excluded": excluded[:20]}


# ---------------------------------------------------------------------------
# Automation Score
# ---------------------------------------------------------------------------


@router.get("/affiliate-intelligence/automation-score")
async def automation_score(authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_founder_os")
    try:
        products = supabase.table(PRODUCT_TABLE).select("status,link_last_checked_at,ai_reviewed").execute().data or []
    except Exception:
        products = []
    try:
        duplicate_candidates = supabase.table(DUPLICATE_TABLE).select("status").execute().data or []
    except Exception:
        duplicate_candidates = []

    auto_checked_links = sum(1 for p in products if p.get("link_last_checked_at"))
    auto_paused = sum(1 for p in products if p.get("status") == "paused")
    auto_reviewed = sum(1 for p in products if p.get("ai_reviewed"))
    auto_duplicates_detected = len(duplicate_candidates)

    manual_needed = sum(1 for p in products if p.get("status") in ("needs_review", "in_review"))
    manual_needed += sum(1 for d in duplicate_candidates if d.get("status") == "moegliches_duplikat")

    automatic_total = auto_checked_links + auto_paused + auto_duplicates_detected
    total = automatic_total + manual_needed
    automation_pct = round(automatic_total / total * 100) if total else None

    return {
        "auto_checked_links": auto_checked_links,
        "auto_paused_products": auto_paused,
        "auto_reviewed_products": auto_reviewed,
        "auto_detected_duplicates": auto_duplicates_detected,
        "manual_decisions_required": manual_needed,
        "automation_percentage": automation_pct,
        "note": "Berechnet aus echten Produkt-/Dublettenzahlen — kein fester Wert." if total else "Noch keine Produktdaten vorhanden.",
    }
