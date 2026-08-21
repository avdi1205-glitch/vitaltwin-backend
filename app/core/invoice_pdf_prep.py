"""Invoice PDF generation — PREPARED, NOT ACTIVATED.

Buchhaltungs-Grundlage, 2026-08-21 §5. This module intentionally
contains no PDF-rendering code and is wired into NO router/webhook — it
only defines the data shape a future invoice would need plus a hard
guard that fails loudly if anything ever tries to call it before it is
genuinely ready. No PDF library (reportlab/weasyprint/etc.) has been
added as a dependency — choosing one is a deliberately deferred decision,
not an oversight.

DO NOT ACTIVATE until BOTH are true:
  1. Gewerbe ist angemeldet (Stand 2026-08-21: noch nicht angemeldet).
  2. Eine echte Steuernummer/USt-ID liegt vor (`CompanyTaxDetails` below
     — both fields are deliberately `None`, never fabricated).

Until then, Stripe's own hosted invoices (see `routers/payments.py` —
no `automatic_tax`/`invoice_creation` configured either, plain
subscription checkout only) remain the only real invoices in this
system. AdSense needs no invoice from us at all — Google issues its own
statements; `core/adsense_billing.py` only records earnings, it never
generates a document.
"""

from __future__ import annotations

from dataclasses import dataclass

# Hard off-switch. Do not flip to True without both preconditions above —
# see the module docstring.
INVOICE_PDF_GENERATION_ENABLED = False


@dataclass(frozen=True)
class CompanyTaxDetails:
    """Placeholder shape for the founder's own tax identity on a future
    invoice. Defaults are deliberately empty/`None` — never fabricated.
    `kleinunternehmer` defaults to `True` as the statistically likely
    starting point for a brand-new single-founder Gewerbe (no USt
    ausgewiesen, §19 UStG), but this is NOT a legal determination made by
    this code — confirm with Finanzamt/Steuerberater before relying on it."""

    steuernummer: str | None = None
    ust_id: str | None = None
    kleinunternehmer: bool = True
    firmenname: str | None = None
    anschrift: str | None = None


def render_invoice_pdf(payment: dict, company: CompanyTaxDetails) -> bytes:
    """Would render a PDF invoice for one `vt_stripe_payments` row. Raises
    unconditionally until `INVOICE_PDF_GENERATION_ENABLED` is manually
    flipped to `True` (see module docstring) — there is deliberately no
    PDF library wired in yet, so this cannot silently produce a document
    with blank or placeholder tax IDs."""
    if not INVOICE_PDF_GENERATION_ENABLED:
        raise RuntimeError(
            "Rechnungs-PDF-Generierung ist absichtlich deaktiviert (siehe "
            "core/invoice_pdf_prep.py) — Gewerbeanmeldung + Steuernummer stehen noch aus."
        )
    raise NotImplementedError("PDF-Rendering-Bibliothek ist noch nicht ausgewählt/integriert.")
