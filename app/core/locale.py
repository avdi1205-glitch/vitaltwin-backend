"""Minimal DE/EN locale resolution for the small set of backend-generated
customer-visible strings not covered by the frontend's next-intl catalog.

This is deliberately NOT a general backend i18n framework — only "de" and
"en" are supported, matching the frontend's own two supported locales, and
resolution falls back to German (the frontend's own default locale) for any
missing/unrecognized value, matching the frontend's default-locale convention.
"""

from __future__ import annotations

SUPPORTED_LOCALES = ("de", "en")
DEFAULT_LOCALE = "de"


def resolve_locale(locale: str | None) -> str:
    """Normalizes a client-supplied locale hint to a supported value."""
    if locale:
        normalized = locale.strip().lower()
        if normalized in SUPPORTED_LOCALES:
            return normalized
    return DEFAULT_LOCALE
