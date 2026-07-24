"""Platform Foundation & Integration Architecture — VitalTwin Release 0.

Single source of truth for the status of every integration category named
in the platform foundation spec (health connectors, payment providers,
affiliate networks, login providers, AI providers, notification channels,
client platforms). This module contains **no vendor SDK calls and no
simulated connections** — every entry is either:

- **implemented + configured**: real, working code path exists and the
  required credentials/env vars are present (verified by reading the
  actual env var, never hardcoded to `True`).
- **implemented, not configured**: real code exists (e.g. Stripe Checkout,
  Google Sign-In) but the required credentials are currently missing.
- **not implemented**: no code path exists yet. Architecture is prepared
  (this registry entry, the docs in `frontend/docs/CONNECTORS.md` /
  `INTEGRATIONS.md`), but calling it would be dishonest — so there is
  nothing to call. The Admin UI must render this as "Noch nicht
  eingerichtet", never as a working feature.

See `frontend/docs/PLATFORM_ARCHITECTURE.md` for the full narrative and
`frontend/docs/API_KEYS.md` for exactly which env var unlocks which entry.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Literal

Status = Literal["configured", "not_configured", "not_implemented"]


@dataclass(frozen=True)
class IntegrationInfo:
    id: str
    name: str
    category: str
    status: Status
    implemented: bool
    required_env_vars: tuple[str, ...] = field(default_factory=tuple)
    note: str = ""


def _env_present(*names: str) -> bool:
    return all(os.getenv(name, "").strip() for name in names)


# ---------------------------------------------------------------------------
# Health & Wearable Connectors
# ---------------------------------------------------------------------------


def get_health_connectors() -> list[IntegrationInfo]:
    """None of these are implemented yet — no connector code, no OAuth flow,
    no data-sync job exists in this codebase. Listed here so the Admin UI
    and docs have one real place to track them as they get built, instead
    of silently forgetting which ones were ever promised."""
    definitions = [
        ("apple_health", "Apple Health (HealthKit)", "Nativer iOS-Connector über HealthKit-Framework — erfordert eine echte iOS-App (Capacitor-iOS-Projekt existiert noch nicht)."),
        ("google_health_connect", "Google Health Connect", "Android-Connector über die Health-Connect-API — erfordert Play-Console-Freigabe für Health-Connect-Zugriff."),
        ("fitbit", "Fitbit", "OAuth2-basierte Web-API (fitbit.com/dev) — erfordert eine registrierte Fitbit-App und Client-Credentials."),
        ("garmin", "Garmin", "Garmin Connect Developer Program (Antragspflichtig, kein Self-Service-Signup)."),
        ("oura", "Oura Ring", "OAuth2 über Oura Cloud API — erfordert eine registrierte Oura-App."),
        ("polar", "Polar", "Polar AccessLink API (OAuth2) — erfordert eine registrierte Polar-App."),
        ("withings", "Withings", "Withings Health API (OAuth2) — erfordert eine registrierte Withings-App."),
        ("abbott_libre", "Abbott Libre (LibreView)", "Kein offizielles Public-API-Self-Signup bekannt — erfordert Partnerschaft mit Abbott."),
        ("dexcom", "Dexcom", "Dexcom Developer API (Sandbox + Production-Freigabe nötig)."),
    ]
    return [
        IntegrationInfo(
            id=identifier,
            name=name,
            category="health_connector",
            status="not_implemented",
            implemented=False,
            note=note,
        )
        for identifier, name, note in definitions
    ]


# ---------------------------------------------------------------------------
# Payment Providers
# ---------------------------------------------------------------------------


def get_payment_providers() -> list[IntegrationInfo]:
    stripe_configured = _env_present("STRIPE_SECRET_KEY")
    return [
        IntegrationInfo(
            id="stripe",
            name="Stripe",
            category="payment_provider",
            status="configured" if stripe_configured else "not_configured",
            implemented=True,
            required_env_vars=("STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET"),
            note=(
                "Implementiert: Abonnements (routers/payments.py). Nicht implementiert: "
                "Einmalzahlungen, Gutscheine/Rabattcodes, Rechnungsstellung."
            ),
        ),
        IntegrationInfo(
            id="paypal",
            name="PayPal",
            category="payment_provider",
            status="not_implemented",
            implemented=False,
            required_env_vars=("PAYPAL_CLIENT_ID", "PAYPAL_CLIENT_SECRET"),
            note="Kein Code-Pfad vorhanden — Architektur vorbereitet über dieselbe Payment-Provider-Kategorie wie Stripe.",
        ),
    ]


# ---------------------------------------------------------------------------
# Affiliate Networks
# ---------------------------------------------------------------------------


def get_affiliate_networks() -> list[IntegrationInfo]:
    definitions = [
        ("amazon_partnernet", "Amazon PartnerNet"),
        ("awin", "Awin"),
        ("digistore24", "Digistore24"),
        ("cj_affiliate", "CJ Affiliate"),
        ("impact", "Impact"),
        ("tradedoubler", "TradeDoubler"),
    ]
    return [
        IntegrationInfo(
            id=identifier,
            name=name,
            category="affiliate_network",
            status="not_implemented",
            implemented=False,
            note=(
                "Kein Netzwerk-API-Zugang angebunden (kein automatischer Produkt-Import, kein automatischer "
                "Provisions-Abgleich). Das generische Affiliate-Management-System selbst (Produkte, Freigabe-"
                "Workflow, Tracking, Analytics, Blacklist, A/B-Tests) ist real implementiert — siehe "
                "routers/affiliate_admin.py und routers/affiliate.py — Partnerprogramme wie dieses werden manuell "
                "im Admin-Bereich unter Partnerprogramme angelegt und gepflegt."
            ),
        )
        for identifier, name in definitions
    ]


# ---------------------------------------------------------------------------
# Login Providers
# ---------------------------------------------------------------------------


def get_auth_providers() -> list[IntegrationInfo]:
    google_configured = _env_present("GOOGLE_CLIENT_ID")
    return [
        IntegrationInfo(
            id="email", name="E-Mail + Passwort", category="auth_provider",
            status="configured", implemented=True,
            note="bcrypt-gehashte Passwörter, siehe routers/users.py::register/login.",
        ),
        IntegrationInfo(
            id="google", name="Google Sign-In", category="auth_provider",
            status="configured" if google_configured else "not_configured", implemented=True,
            required_env_vars=("GOOGLE_CLIENT_ID",),
            note="Code vorhanden (routers/users.py::google_login), aktiv sobald GOOGLE_CLIENT_ID gesetzt ist.",
        ),
        IntegrationInfo(
            id="apple", name="Sign in with Apple", category="auth_provider",
            status="not_implemented", implemented=False,
            required_env_vars=("APPLE_CLIENT_ID", "APPLE_TEAM_ID", "APPLE_KEY_ID"),
        ),
        IntegrationInfo(
            id="microsoft", name="Microsoft Login", category="auth_provider",
            status="not_implemented", implemented=False,
            required_env_vars=("MICROSOFT_CLIENT_ID", "MICROSOFT_CLIENT_SECRET"),
        ),
        IntegrationInfo(
            id="passkeys", name="Passkeys (WebAuthn)", category="auth_provider",
            status="not_implemented", implemented=False,
            note="Erfordert WebAuthn-Relying-Party-Setup (kein Drittanbieter-Key nötig, aber eigene Implementierung).",
        ),
    ]


# ---------------------------------------------------------------------------
# AI Providers
# ---------------------------------------------------------------------------


def get_ai_providers() -> list[IntegrationInfo]:
    openai_configured = _env_present("OPENAI_API_KEY")
    return [
        IntegrationInfo(
            id="openai", name="OpenAI", category="ai_provider",
            status="configured" if openai_configured else "not_configured", implemented=True,
            required_env_vars=("OPENAI_API_KEY",),
            note="services/ai_provider.py::OpenAIProvider — einzige aktive Implementierung der AIProvider-Schnittstelle.",
        ),
        IntegrationInfo(
            id="anthropic", name="Anthropic Claude", category="ai_provider",
            status="not_implemented", implemented=False,
            required_env_vars=("ANTHROPIC_API_KEY",),
            note="Kein AIProvider-Subclass vorhanden. Schnittstelle (services/ai_provider.py::AIProvider) ist bereit für eine zweite Implementierung.",
        ),
        IntegrationInfo(
            id="gemini", name="Google Gemini", category="ai_provider",
            status="not_implemented", implemented=False,
            required_env_vars=("GEMINI_API_KEY",),
            note="Kein AIProvider-Subclass vorhanden. Gleiche Schnittstelle wie oben.",
        ),
    ]


# ---------------------------------------------------------------------------
# Notification Channels
# ---------------------------------------------------------------------------


def get_notification_channels() -> list[IntegrationInfo]:
    smtp_configured = _env_present("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD")
    return [
        IntegrationInfo(
            id="in_app", name="In-App-Benachrichtigungen", category="notification_channel",
            status="configured", implemented=True,
            note="vt_notifications-Tabelle + routers/notifications.py.",
        ),
        IntegrationInfo(
            id="email_transactional", name="Transaktions-E-Mail", category="notification_channel",
            status="configured" if smtp_configured else "not_configured", implemented=True,
            required_env_vars=("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD"),
            note="Aktuell nur für das Kontaktformular genutzt (routers/contact.py) — kein generisches Transaktions-Mail-System.",
        ),
        IntegrationInfo(
            id="push", name="Push-Benachrichtigungen", category="notification_channel",
            status="not_implemented", implemented=False,
            required_env_vars=("FCM_SERVER_KEY",),
            note="Kein Push-Provider (z. B. Firebase Cloud Messaging) angebunden.",
        ),
        IntegrationInfo(
            id="newsletter", name="Newsletter", category="notification_channel",
            status="not_implemented", implemented=False,
            note="Kein Massen-Mail-/Newsletter-System (z. B. Mailchimp/Brevo) angebunden.",
        ),
    ]


# ---------------------------------------------------------------------------
# Client Platforms
# ---------------------------------------------------------------------------


def get_platforms() -> list[IntegrationInfo]:
    return [
        IntegrationInfo(id="web", name="Web", category="platform", status="configured", implemented=True,
                         note="Next.js, responsive (Tailwind)."),
        IntegrationInfo(id="tablet", name="Tablet", category="platform", status="configured", implemented=True,
                         note="Abgedeckt durch responsives Web-Layout, kein separater Build."),
        IntegrationInfo(id="android", name="Android", category="platform", status="configured", implemented=True,
                         note="Capacitor-WebView-Wrapper (frontend/android) lädt vitaltwin.de — kein natives Health-Connect-SDK eingebunden."),
        IntegrationInfo(id="ios", name="iOS", category="platform", status="not_implemented", implemented=False,
                         note="Kein Capacitor-iOS-Projekt vorhanden (erfordert macOS + Xcode zum Hinzufügen)."),
    ]


def get_full_integration_report() -> dict[str, list[dict[str, object]]]:
    def _serialize(items: list[IntegrationInfo]) -> list[dict[str, object]]:
        return [
            {
                "id": item.id,
                "name": item.name,
                "category": item.category,
                "status": item.status,
                "implemented": item.implemented,
                "required_env_vars": list(item.required_env_vars),
                "note": item.note,
            }
            for item in items
        ]

    return {
        "platforms": _serialize(get_platforms()),
        "health_connectors": _serialize(get_health_connectors()),
        "payment_providers": _serialize(get_payment_providers()),
        "affiliate_networks": _serialize(get_affiliate_networks()),
        "auth_providers": _serialize(get_auth_providers()),
        "ai_providers": _serialize(get_ai_providers()),
        "notification_channels": _serialize(get_notification_channels()),
    }
