"""Affiliate Center — Admin API (VitalTwin Enterprise Release —
Affiliate Intelligence & Management Platform).

Mounted at `/api/admin/affiliate` in `app/main.py`. Every endpoint calls
`core/admin_rbac.py::require_admin_permission` first, exactly like every
other admin router in this codebase — no exception for "just reading a
list".

Sections (matching the requested spec 1:1):

- `/dashboard`            Dashboard (real KPIs, computed from actual rows)
- `/partners*`            Partnerprogramme
- `/categories*`          Kategorien
- `/products*`            Produkte (incl. `/products/{id}/check-link`)
- `/blacklist*`           Blacklist
- `/campaigns*`           Kampagnen (saisonal)
- `/ab-tests*`            A/B Testing
- `/events`               Tracking (read-only list; writes happen via the
                          public `routers/affiliate.py::track_event`)
- `/analytics`            Analytics (Top-Produkte/-Kategorien/-Partner)
- `/commissions`          Provisionen
- `/import`               Import (CSV/JSON/Excel)
- `/export`               Export (CSV/JSON/Excel)
- `/settings`             Einstellungen (liest/schreibt den bestehenden
                          Feature-Flag `affiliate_recommendations_enabled`
                          — keine separate Einstellungs-Tabelle noetig)

**Only `status in ("approved", "active")` products are ever eligible for
recommendation** — enforced in `core/affiliate_engine.py`, not here; this
router only manages the data, it never decides what gets shown to a user.
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone

from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, field_validator

from ..core.admin_rbac import require_admin_permission
from ..core.affiliate_import_export import export_products, import_products
from ..core.affiliate_link_checker import check_link
from ..core.audit import record_audit_event
from ..core.supabase import supabase

router = APIRouter()

PARTNER_TABLE = "vt_affiliate_partners"
CATEGORY_TABLE = "vt_affiliate_categories"
PRODUCT_TABLE = "vt_affiliate_products"
BLACKLIST_TABLE = "vt_affiliate_blacklist"
CAMPAIGN_TABLE = "vt_affiliate_campaigns"
AB_TEST_TABLE = "vt_affiliate_ab_tests"
EVENT_TABLE = "vt_affiliate_events"
FEATURE_FLAG_TABLE = "vt_feature_flags"

ALLOWED_PRODUCT_STATUSES = {
    "draft", "in_review", "approved", "active", "paused", "expired", "archived",
}
ELIGIBLE_STATUSES = {"approved", "active"}
ALLOWED_BLACKLIST_TYPES = {"product", "brand", "partner", "category"}
ALLOWED_EXPORT_FORMATS = {"csv", "json", "xlsx"}
ALLOWED_IMPORT_FORMATS = {"csv", "json", "xlsx"}
RECOMMENDATIONS_FLAG_KEY = "affiliate_recommendations_enabled"


# ---------------------------------------------------------------------------
# Input models
# ---------------------------------------------------------------------------


class PartnerInput(BaseModel):
    network: str
    partner_name: str
    partner_code: str
    status: str = "inactive"
    api_available: bool = False
    api_key: str | None = None
    tracking_id: str | None = None
    commission_rate: float | None = None
    cookie_duration_days: int | None = None
    notes: str | None = None


class CategoryInput(BaseModel):
    name: str
    slug: str


class ProductInput(BaseModel):
    title: str
    subtitle: str | None = None
    category_id: str | None = None
    brand: str | None = None
    manufacturer: str | None = None
    description: str | None = None
    image_url: str | None = None
    price: float | None = None
    currency: str = "eur"
    affiliate_url: str
    deep_link: str | None = None
    partner_id: str | None = None
    commission_rate: float | None = None
    tags: list[str] = []
    target_audience: str | None = None
    region: str = "DE"
    language: str = "de"
    status: str = "draft"
    priority: int = 0
    rating: float | None = None
    notes: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    pinned: bool = False

    @field_validator("status")
    @classmethod
    def _validate_status(cls, value: str) -> str:
        if value not in ALLOWED_PRODUCT_STATUSES:
            raise ValueError(f"Ungültiger Status. Erlaubt: {', '.join(sorted(ALLOWED_PRODUCT_STATUSES))}")
        return value


class ProductStatusInput(BaseModel):
    status: str

    @field_validator("status")
    @classmethod
    def _validate_status(cls, value: str) -> str:
        if value not in ALLOWED_PRODUCT_STATUSES:
            raise ValueError(f"Ungültiger Status. Erlaubt: {', '.join(sorted(ALLOWED_PRODUCT_STATUSES))}")
        return value


class BlacklistInput(BaseModel):
    entry_type: str
    value: str
    reason: str | None = None

    @field_validator("entry_type")
    @classmethod
    def _validate_type(cls, value: str) -> str:
        if value not in ALLOWED_BLACKLIST_TYPES:
            raise ValueError(f"Ungültiger Typ. Erlaubt: {', '.join(sorted(ALLOWED_BLACKLIST_TYPES))}")
        return value


class CampaignInput(BaseModel):
    name: str
    season: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    product_ids: list[str] = []
    active: bool = True


class AbTestInput(BaseModel):
    name: str
    product_a_id: str
    product_b_id: str


class ImportInput(BaseModel):
    format: str
    content: str  # raw text for csv/json, base64 for xlsx

    @field_validator("format")
    @classmethod
    def _validate_format(cls, value: str) -> str:
        if value not in ALLOWED_IMPORT_FORMATS:
            raise ValueError(f"Ungültiges Format. Erlaubt: {', '.join(sorted(ALLOWED_IMPORT_FORMATS))}")
        return value


class SettingsInput(BaseModel):
    recommendations_enabled: bool


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


@router.get("/dashboard")
async def affiliate_dashboard(authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_affiliate")
    try:
        products = supabase.table(PRODUCT_TABLE).select("id,status,link_status").execute().data or []
    except Exception:
        products = []
    try:
        partners = supabase.table(PARTNER_TABLE).select("id,status").execute().data or []
    except Exception:
        partners = []

    status_counts: dict[str, int] = {}
    for product in products:
        status = product.get("status", "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
    broken_links = sum(1 for p in products if p.get("link_status") == "broken")
    eligible_count = sum(1 for p in products if p.get("status") in ELIGIBLE_STATUSES)

    return {
        "total_products": len(products),
        "products_by_status": status_counts,
        "eligible_for_recommendation": eligible_count,
        "broken_links": broken_links,
        "total_partners": len(partners),
        "active_partners": sum(1 for p in partners if p.get("status") == "active"),
    }


# ---------------------------------------------------------------------------
# Partnerprogramme
# ---------------------------------------------------------------------------


@router.get("/partners")
async def list_partners(authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_affiliate")
    try:
        rows = supabase.table(PARTNER_TABLE).select("*").order("network").execute().data or []
    except Exception:
        rows = []
    return {"items": rows}


@router.post("/partners")
async def create_partner(data: PartnerInput, authorization: str | None = Header(default=None)):
    admin = require_admin_permission(authorization, "manage_affiliate")
    try:
        response = supabase.table(PARTNER_TABLE).insert(data.model_dump()).execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Partnerprogramm konnte nicht gespeichert werden.") from exc
    record_audit_event(user_id=None, email=admin.email, action="create", entity_type="affiliate_partner")
    return response.data[0] if response.data else data.model_dump()


@router.patch("/partners/{partner_id}")
async def update_partner(partner_id: str, data: PartnerInput, authorization: str | None = Header(default=None)):
    admin = require_admin_permission(authorization, "manage_affiliate")
    payload = data.model_dump()
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    try:
        supabase.table(PARTNER_TABLE).update(payload).eq("id", partner_id).execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Partnerprogramm konnte nicht aktualisiert werden.") from exc
    record_audit_event(user_id=None, email=admin.email, action="update", entity_type="affiliate_partner", entity_id=partner_id)
    return {"message": "Partnerprogramm aktualisiert."}


@router.delete("/partners/{partner_id}")
async def delete_partner(partner_id: str, authorization: str | None = Header(default=None)):
    admin = require_admin_permission(authorization, "manage_affiliate")
    try:
        supabase.table(PARTNER_TABLE).delete().eq("id", partner_id).execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Partnerprogramm konnte nicht gelöscht werden.") from exc
    record_audit_event(user_id=None, email=admin.email, action="delete", entity_type="affiliate_partner", entity_id=partner_id)
    return {"message": "Partnerprogramm gelöscht."}


# ---------------------------------------------------------------------------
# Kategorien
# ---------------------------------------------------------------------------


@router.get("/categories")
async def list_categories(authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_affiliate")
    try:
        rows = supabase.table(CATEGORY_TABLE).select("*").order("name").execute().data or []
    except Exception:
        rows = []
    return {"items": rows}


@router.post("/categories")
async def create_category(data: CategoryInput, authorization: str | None = Header(default=None)):
    admin = require_admin_permission(authorization, "manage_affiliate")
    try:
        response = supabase.table(CATEGORY_TABLE).insert(data.model_dump()).execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Kategorie konnte nicht gespeichert werden.") from exc
    record_audit_event(user_id=None, email=admin.email, action="create", entity_type="affiliate_category")
    return response.data[0] if response.data else data.model_dump()


@router.delete("/categories/{category_id}")
async def delete_category(category_id: str, authorization: str | None = Header(default=None)):
    admin = require_admin_permission(authorization, "manage_affiliate")
    try:
        supabase.table(CATEGORY_TABLE).delete().eq("id", category_id).execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Kategorie konnte nicht gelöscht werden.") from exc
    record_audit_event(user_id=None, email=admin.email, action="delete", entity_type="affiliate_category", entity_id=category_id)
    return {"message": "Kategorie gelöscht."}


# ---------------------------------------------------------------------------
# Produkte
# ---------------------------------------------------------------------------


@router.get("/products")
async def list_products(status: str | None = None, authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_affiliate")
    try:
        query = supabase.table(PRODUCT_TABLE).select("*")
        if status:
            query = query.eq("status", status)
        rows = query.order("created_at", desc=True).execute().data or []
    except Exception:
        rows = []
    return {"items": rows}


@router.post("/products")
async def create_product(data: ProductInput, authorization: str | None = Header(default=None)):
    admin = require_admin_permission(authorization, "manage_affiliate")
    payload = data.model_dump()
    payload["created_by"] = admin.email
    try:
        response = supabase.table(PRODUCT_TABLE).insert(payload).execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Produkt konnte nicht gespeichert werden.") from exc
    record_audit_event(user_id=None, email=admin.email, action="create", entity_type="affiliate_product", metadata={"status": data.status})
    return response.data[0] if response.data else payload


@router.patch("/products/{product_id}")
async def update_product(product_id: str, data: ProductInput, authorization: str | None = Header(default=None)):
    admin = require_admin_permission(authorization, "manage_affiliate")
    payload = data.model_dump()
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    try:
        supabase.table(PRODUCT_TABLE).update(payload).eq("id", product_id).execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Produkt konnte nicht aktualisiert werden.") from exc
    record_audit_event(user_id=None, email=admin.email, action="update", entity_type="affiliate_product", entity_id=product_id)
    return {"message": "Produkt aktualisiert."}


@router.patch("/products/{product_id}/status")
async def update_product_status(product_id: str, data: ProductStatusInput, authorization: str | None = Header(default=None)):
    """Dedicated status-transition endpoint for the approval workflow
    (Entwurf -> In Pruefung -> Freigegeben -> Aktiv -> Pausiert/Abgelaufen/
    Archiviert) — same permission as any other product edit, but logged
    with the specific `status` in the audit metadata for a clean approval
    trail."""
    admin = require_admin_permission(authorization, "manage_affiliate")
    try:
        supabase.table(PRODUCT_TABLE).update(
            {"status": data.status, "updated_at": datetime.now(timezone.utc).isoformat()}
        ).eq("id", product_id).execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Status konnte nicht geändert werden.") from exc
    record_audit_event(
        user_id=None, email=admin.email, action="update", entity_type="affiliate_product", entity_id=product_id,
        metadata={"status": data.status},
    )
    return {"message": "Status aktualisiert."}


@router.delete("/products/{product_id}")
async def delete_product(product_id: str, authorization: str | None = Header(default=None)):
    admin = require_admin_permission(authorization, "manage_affiliate")
    try:
        supabase.table(PRODUCT_TABLE).delete().eq("id", product_id).execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Produkt konnte nicht gelöscht werden.") from exc
    record_audit_event(user_id=None, email=admin.email, action="delete", entity_type="affiliate_product", entity_id=product_id)
    return {"message": "Produkt gelöscht."}


@router.post("/products/{product_id}/check-link")
async def check_product_link(product_id: str, authorization: str | None = Header(default=None)):
    """Performs one real HTTP request against the product's
    `affiliate_url` right now (see `core/affiliate_link_checker.py`) — not
    a scheduled/background job, since none exists in this codebase."""
    admin = require_admin_permission(authorization, "manage_affiliate")
    try:
        rows = supabase.table(PRODUCT_TABLE).select("id,affiliate_url").eq("id", product_id).limit(1).execute().data or []
    except Exception:
        rows = []
    if not rows:
        raise HTTPException(status_code=404, detail="Produkt nicht gefunden.")

    result = check_link(rows[0]["affiliate_url"])
    try:
        supabase.table(PRODUCT_TABLE).update(
            {
                "link_status": result["link_status"],
                "link_http_status": result["http_status"],
                "link_last_checked_at": datetime.now(timezone.utc).isoformat(),
            }
        ).eq("id", product_id).execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Ergebnis konnte nicht gespeichert werden.") from exc

    record_audit_event(
        user_id=None, email=admin.email, action="update", entity_type="affiliate_product", entity_id=product_id,
        metadata={"link_check": result},
    )
    return result


# ---------------------------------------------------------------------------
# Blacklist
# ---------------------------------------------------------------------------


@router.get("/blacklist")
async def list_blacklist(authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_affiliate")
    try:
        rows = supabase.table(BLACKLIST_TABLE).select("*").order("created_at", desc=True).execute().data or []
    except Exception:
        rows = []
    return {"items": rows}


@router.post("/blacklist")
async def create_blacklist_entry(data: BlacklistInput, authorization: str | None = Header(default=None)):
    admin = require_admin_permission(authorization, "manage_affiliate")
    payload = data.model_dump()
    payload["created_by"] = admin.email
    try:
        response = supabase.table(BLACKLIST_TABLE).insert(payload).execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Eintrag konnte nicht gespeichert werden.") from exc
    record_audit_event(user_id=None, email=admin.email, action="create", entity_type="affiliate_blacklist", metadata={"entry_type": data.entry_type})
    return response.data[0] if response.data else payload


@router.delete("/blacklist/{entry_id}")
async def delete_blacklist_entry(entry_id: str, authorization: str | None = Header(default=None)):
    admin = require_admin_permission(authorization, "manage_affiliate")
    try:
        supabase.table(BLACKLIST_TABLE).delete().eq("id", entry_id).execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Eintrag konnte nicht gelöscht werden.") from exc
    record_audit_event(user_id=None, email=admin.email, action="delete", entity_type="affiliate_blacklist", entity_id=entry_id)
    return {"message": "Eintrag gelöscht."}


# ---------------------------------------------------------------------------
# Kampagnen (saisonal)
# ---------------------------------------------------------------------------


@router.get("/campaigns")
async def list_campaigns(authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_affiliate")
    try:
        rows = supabase.table(CAMPAIGN_TABLE).select("*").order("created_at", desc=True).execute().data or []
    except Exception:
        rows = []
    return {"items": rows}


@router.post("/campaigns")
async def create_campaign(data: CampaignInput, authorization: str | None = Header(default=None)):
    admin = require_admin_permission(authorization, "manage_affiliate")
    payload = data.model_dump()
    payload["created_by"] = admin.email
    try:
        response = supabase.table(CAMPAIGN_TABLE).insert(payload).execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Kampagne konnte nicht gespeichert werden.") from exc
    record_audit_event(user_id=None, email=admin.email, action="create", entity_type="affiliate_campaign")
    return response.data[0] if response.data else payload


@router.patch("/campaigns/{campaign_id}")
async def update_campaign(campaign_id: str, data: CampaignInput, authorization: str | None = Header(default=None)):
    admin = require_admin_permission(authorization, "manage_affiliate")
    try:
        supabase.table(CAMPAIGN_TABLE).update(data.model_dump()).eq("id", campaign_id).execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Kampagne konnte nicht aktualisiert werden.") from exc
    record_audit_event(user_id=None, email=admin.email, action="update", entity_type="affiliate_campaign", entity_id=campaign_id)
    return {"message": "Kampagne aktualisiert."}


@router.delete("/campaigns/{campaign_id}")
async def delete_campaign(campaign_id: str, authorization: str | None = Header(default=None)):
    admin = require_admin_permission(authorization, "manage_affiliate")
    try:
        supabase.table(CAMPAIGN_TABLE).delete().eq("id", campaign_id).execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Kampagne konnte nicht gelöscht werden.") from exc
    record_audit_event(user_id=None, email=admin.email, action="delete", entity_type="affiliate_campaign", entity_id=campaign_id)
    return {"message": "Kampagne gelöscht."}


# ---------------------------------------------------------------------------
# A/B Testing
# ---------------------------------------------------------------------------


@router.get("/ab-tests")
async def list_ab_tests(authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_affiliate")
    try:
        tests = supabase.table(AB_TEST_TABLE).select("*").order("created_at", desc=True).execute().data or []
    except Exception:
        tests = []

    for test in tests:
        try:
            events = (
                supabase.table(EVENT_TABLE)
                .select("product_id,event_type,revenue")
                .eq("ab_test_id", test["id"])
                .execute()
                .data
                or []
            )
        except Exception:
            events = []
        for variant_key, product_id in (("a", test.get("product_a_id")), ("b", test.get("product_b_id"))):
            variant_events = [e for e in events if str(e.get("product_id")) == str(product_id)]
            test[f"impressions_{variant_key}"] = sum(1 for e in variant_events if e["event_type"] == "impression")
            test[f"clicks_{variant_key}"] = sum(1 for e in variant_events if e["event_type"] == "click")
            test[f"conversions_{variant_key}"] = sum(1 for e in variant_events if e["event_type"] == "conversion")
            test[f"revenue_{variant_key}"] = sum(e.get("revenue") or 0 for e in variant_events if e["event_type"] == "conversion")
    return {"items": tests}


@router.post("/ab-tests")
async def create_ab_test(data: AbTestInput, authorization: str | None = Header(default=None)):
    admin = require_admin_permission(authorization, "manage_affiliate")
    payload = data.model_dump()
    payload["created_by"] = admin.email
    try:
        response = supabase.table(AB_TEST_TABLE).insert(payload).execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail="A/B-Test konnte nicht gespeichert werden.") from exc
    record_audit_event(user_id=None, email=admin.email, action="create", entity_type="affiliate_ab_test")
    return response.data[0] if response.data else payload


@router.post("/ab-tests/{test_id}/complete")
async def complete_ab_test(test_id: str, authorization: str | None = Header(default=None)):
    """Picks the winner based on real recorded conversions (higher
    conversion count wins; ties are reported as `"tie"`, never guessed)."""
    admin = require_admin_permission(authorization, "manage_affiliate")
    try:
        rows = supabase.table(AB_TEST_TABLE).select("*").eq("id", test_id).limit(1).execute().data or []
    except Exception:
        rows = []
    if not rows:
        raise HTTPException(status_code=404, detail="A/B-Test nicht gefunden.")
    test = rows[0]

    try:
        events = supabase.table(EVENT_TABLE).select("product_id,event_type").eq("ab_test_id", test_id).eq("event_type", "conversion").execute().data or []
    except Exception:
        events = []
    conversions_a = sum(1 for e in events if str(e.get("product_id")) == str(test.get("product_a_id")))
    conversions_b = sum(1 for e in events if str(e.get("product_id")) == str(test.get("product_b_id")))
    winner = "a" if conversions_a > conversions_b else "b" if conversions_b > conversions_a else "tie"

    try:
        supabase.table(AB_TEST_TABLE).update({"status": "completed", "winner": winner}).eq("id", test_id).execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Ergebnis konnte nicht gespeichert werden.") from exc

    record_audit_event(user_id=None, email=admin.email, action="update", entity_type="affiliate_ab_test", entity_id=test_id, metadata={"winner": winner})
    return {"winner": winner, "conversions_a": conversions_a, "conversions_b": conversions_b}


# ---------------------------------------------------------------------------
# Tracking (read-only list — writes happen via routers/affiliate.py)
# ---------------------------------------------------------------------------


@router.get("/events")
async def list_events(
    event_type: str | None = None,
    product_id: str | None = None,
    limit: int = Query(default=50, le=200),
    authorization: str | None = Header(default=None),
):
    require_admin_permission(authorization, "view_affiliate")
    try:
        query = supabase.table(EVENT_TABLE).select("*")
        if event_type:
            query = query.eq("event_type", event_type)
        if product_id:
            query = query.eq("product_id", product_id)
        rows = query.order("created_at", desc=True).limit(limit).execute().data or []
    except Exception:
        rows = []
    return {"items": rows}


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------


@router.get("/analytics")
async def affiliate_analytics(authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_affiliate")
    try:
        events = supabase.table(EVENT_TABLE).select("*").execute().data or []
    except Exception:
        events = []
    try:
        products = supabase.table(PRODUCT_TABLE).select("id,title,category_id,partner_id").execute().data or []
    except Exception:
        products = []
    product_by_id = {str(p["id"]): p for p in products}

    per_product: dict[str, dict] = {}
    per_category: dict[str, dict] = {}
    per_partner: dict[str, dict] = {}

    def _bucket(store: dict, key: str):
        return store.setdefault(key, {"impressions": 0, "clicks": 0, "conversions": 0, "revenue": 0.0, "commission": 0.0})

    for event in events:
        product_id = str(event.get("product_id"))
        product = product_by_id.get(product_id, {})
        category_id = str(product.get("category_id")) if product.get("category_id") else "none"
        partner_id = str(product.get("partner_id")) if product.get("partner_id") else "none"

        for store, key in ((per_product, product_id), (per_category, category_id), (per_partner, partner_id)):
            bucket = _bucket(store, key)
            event_type = event.get("event_type")
            if event_type == "impression":
                bucket["impressions"] += 1
            elif event_type == "click":
                bucket["clicks"] += 1
            elif event_type == "conversion":
                bucket["conversions"] += 1
                bucket["revenue"] += float(event.get("revenue") or 0)
                bucket["commission"] += float(event.get("commission") or 0)

    top_products = sorted(
        [{"product_id": k, "title": product_by_id.get(k, {}).get("title", "—"), **v} for k, v in per_product.items()],
        key=lambda x: x["revenue"], reverse=True,
    )[:10]
    top_categories = sorted(
        [{"category_id": k, **v} for k, v in per_category.items()], key=lambda x: x["revenue"], reverse=True
    )[:10]
    top_partners = sorted(
        [{"partner_id": k, **v} for k, v in per_partner.items()], key=lambda x: x["revenue"], reverse=True
    )[:10]

    return {"top_products": top_products, "top_categories": top_categories, "top_partners": top_partners}


@router.get("/commissions")
async def affiliate_commissions(authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_affiliate")
    try:
        events = (
            supabase.table(EVENT_TABLE).select("commission,revenue,created_at").eq("event_type", "conversion").execute().data
            or []
        )
    except Exception:
        events = []
    total_commission = sum(float(e.get("commission") or 0) for e in events)
    total_revenue = sum(float(e.get("revenue") or 0) for e in events)
    return {
        "total_commission": total_commission,
        "total_revenue": total_revenue,
        "conversion_count": len(events),
        "note": (
            "Berechnet aus vt_affiliate_events (event_type='conversion'). Diese Werte werden ausschliesslich "
            "durch echte Tracking-Events befuellt — es gibt keine automatische Zahlungsabgleichung mit den "
            "Partnerprogrammen (kein API-Zugang zu Amazon PartnerNet/Awin/etc., siehe core/integrations.py)."
        ),
    }


# ---------------------------------------------------------------------------
# Import / Export
# ---------------------------------------------------------------------------


@router.post("/import")
async def import_affiliate_products(data: ImportInput, authorization: str | None = Header(default=None)):
    admin = require_admin_permission(authorization, "manage_affiliate")
    try:
        content_bytes = base64.b64decode(data.content) if data.format == "xlsx" else data.content.encode("utf-8")
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Inhalt konnte nicht dekodiert werden.") from exc

    try:
        result = import_products(data.format, content_bytes, created_by=admin.email)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Import fehlgeschlagen: {exc}") from exc

    record_audit_event(
        user_id=None, email=admin.email, action="create", entity_type="affiliate_product_import",
        metadata={"format": data.format, "imported": result["imported"], "errors": len(result["errors"])},
    )
    return result


@router.get("/export")
async def export_affiliate_products(fmt: str = Query(default="csv", alias="format"), authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_affiliate")
    if fmt not in ALLOWED_EXPORT_FORMATS:
        raise HTTPException(status_code=400, detail=f"Ungültiges Format. Erlaubt: {', '.join(sorted(ALLOWED_EXPORT_FORMATS))}")
    content, media_type, filename = export_products(fmt)
    return Response(content=content, media_type=media_type, headers={"Content-Disposition": f'attachment; filename="{filename}"'})


# ---------------------------------------------------------------------------
# Einstellungen — reuses the existing Feature-Flag system, no new table.
# ---------------------------------------------------------------------------


@router.get("/settings")
async def get_affiliate_settings(authorization: str | None = Header(default=None)):
    require_admin_permission(authorization, "view_affiliate")
    try:
        rows = (
            supabase.table(FEATURE_FLAG_TABLE).select("*").eq("key", RECOMMENDATIONS_FLAG_KEY).limit(1).execute().data or []
        )
    except Exception:
        rows = []
    enabled = rows[0]["enabled"] if rows else True
    return {"recommendations_enabled": enabled}


@router.put("/settings")
async def update_affiliate_settings(data: SettingsInput, authorization: str | None = Header(default=None)):
    admin = require_admin_permission(authorization, "manage_affiliate")
    payload = {
        "key": RECOMMENDATIONS_FLAG_KEY,
        "enabled": data.recommendations_enabled,
        "description": "Globaler Schalter fuer Affiliate-Produktempfehlungen im Twin/Frontend.",
        "updated_by": admin.email,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        supabase.table(FEATURE_FLAG_TABLE).upsert(payload).execute()
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Einstellung konnte nicht gespeichert werden.") from exc
    record_audit_event(user_id=None, email=admin.email, action="update", entity_type="feature_flag", entity_id=RECOMMENDATIONS_FLAG_KEY)
    return {"message": "Einstellung gespeichert."}
