"""Automation Engine — Safe Action Registry (VitalTwin Enterprise, Founder
Operating System, Submodule G).

This is the **single source of truth** for which automated actions may
ever run, and under what constraints. `core/automation_engine.py` never
executes an arbitrary string — every action a rule references must exist
here, be `implemented=True`, and be allowed for the rule's `environment`.

**No dynamic code execution, no shell commands, no arbitrary tool calls.**
Every entry maps to one specific, hand-written Python function in
`core/automation_engine.py` that calls directly into an existing,
already-reviewed module (`affiliate_link_checker`, `affiliate_admin`
table updates, `founder_business_metrics`, `core.integrations`, the
Task-Manager/Approval-Center tables) — never user-supplied code.

**No `critical`-risk action is defined here, on purpose.** Per spec,
price changes, legal text, account deletion/locking, deployments,
refunds, contracts, and API-key rotation must never be reachable via the
Automation Engine, regardless of what a rule's `risk_level` field claims.
The only way to guarantee that is to never implement them as an action in
the first place — see `CRITICAL_ACTIONS_NOT_IMPLEMENTED_NOTE` below.

**Only actions backed by real, already-existing functionality are
implemented** (same "keine Fake-Daten" principle as every other
Founder-OS detector/engine in this codebase). Several actions named in the
spec (cache invalidation, changelog preparation, documentation
regeneration, support-ticket categorization, background-job restart) have
**no real infrastructure behind them in this codebase** — there is no
cache layer, no changelog system, no doc-generation pipeline, no ticket
category field, no background-job/queue runtime to restart. Implementing
them would mean either doing nothing while claiming success, or inventing
fake state — both violate the mandate. They are therefore intentionally
**absent** from this registry rather than present-but-fake.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

RiskLevel = Literal["low", "medium", "high", "critical"]
Environment = Literal["development", "staging", "production"]

ALL_ENVIRONMENTS: tuple[Environment, ...] = ("development", "staging", "production")

ApprovalPolicy = Literal[
    "no_approval",
    "one_time_approval",
    "always_require_approval",
    "founder_only",
    "super_admin_only",
    "approval_for_threshold",
    "approval_for_new_scope",
]
APPROVAL_POLICIES: frozenset[str] = frozenset(ApprovalPolicy.__args__)  # type: ignore[attr-defined]

TriggerType = Literal[
    "schedule", "manual", "event", "approval_granted", "task_overdue", "threshold",
]
TRIGGER_TYPES: frozenset[str] = frozenset(TriggerType.__args__)  # type: ignore[attr-defined]

RiskOrEnvLiteral = Literal["low", "medium", "high", "critical"]
RISK_LEVELS: frozenset[str] = frozenset(RiskOrEnvLiteral.__args__)  # type: ignore[attr-defined]
RISK_ORDER: dict[str, int] = {"low": 0, "medium": 1, "high": 2, "critical": 3}

AutomationCategory = Literal[
    "affiliate", "business", "analytics", "reports", "support", "seo", "content",
    "dokumentation", "releases", "tests", "backups", "system_monitoring",
    "api_monitoring", "ki_kosten", "performance", "sicherheit", "integrationen",
    "datenqualitaet", "founder_tasks", "founder_briefing",
]
AUTOMATION_CATEGORIES: frozenset[str] = frozenset(AutomationCategory.__args__)  # type: ignore[attr-defined]

# Categories that currently have at least one real, implemented action
# behind them (the rest are valid labels for future use, exactly like the
# 16 `Source` values in `founder_task_detector.py` where only 5 have a
# real detection rule).
CATEGORIES_WITH_REAL_ACTIONS: frozenset[str] = frozenset(
    {"affiliate", "business", "analytics", "reports", "system_monitoring",
     "api_monitoring", "integrationen", "founder_tasks", "founder_briefing"}
)


@dataclass(frozen=True)
class ActionDefinition:
    action_type: str
    label: str
    risk_level: RiskLevel
    required_role: str | None  # None = jede Rolle mit manage_automation_engine; sonst z. B. "super_admin"
    requires_approval_by_default: bool
    idempotent: bool
    reversible: bool
    timeout_seconds: int
    retry_allowed: bool
    allowed_environments: tuple[Environment, ...]
    required_inputs: tuple[str, ...]
    output_schema: tuple[str, ...]
    implemented: bool = True
    note: str = ""


ACTION_REGISTRY: dict[str, ActionDefinition] = {
    "task_erstellen": ActionDefinition(
        action_type="task_erstellen", label="Aufgabe erstellen (Task Manager)", risk_level="low",
        required_role=None, requires_approval_by_default=False, idempotent=True, reversible=False,
        timeout_seconds=10, retry_allowed=True, allowed_environments=ALL_ENVIRONMENTS,
        required_inputs=("title", "category", "reason"), output_schema=("task_id",),
        note="Schreibt idempotent (dedupe_key) in vt_founder_tasks — reuses core/founder_task_detector.py's Upsert-Muster.",
    ),
    "approval_anfordern": ActionDefinition(
        action_type="approval_anfordern", label="Freigabe anfordern (Smart Approval Center)", risk_level="low",
        required_role=None, requires_approval_by_default=False, idempotent=True, reversible=False,
        timeout_seconds=10, retry_allowed=True, allowed_environments=ALL_ENVIRONMENTS,
        required_inputs=("title", "category", "reason"), output_schema=("approval_id",),
        note="Schreibt idempotent in vt_founder_approvals — die Anfrage SELBST ist risikoarm, die Entscheidung trifft immer der Gründer dort.",
    ),
    "link_pruefen": ActionDefinition(
        action_type="link_pruefen", label="Affiliate-Link erneut prüfen", risk_level="low",
        required_role=None, requires_approval_by_default=False, idempotent=True, reversible=False,
        timeout_seconds=15, retry_allowed=True, allowed_environments=ALL_ENVIRONMENTS,
        required_inputs=("product_id",), output_schema=("link_status", "http_status"),
        note="Reuses core/affiliate_link_checker.py::check_link() — echter HTTP-Check, kein Fake.",
    ),
    "affiliate_produkt_pausieren": ActionDefinition(
        action_type="affiliate_produkt_pausieren", label="Affiliate-Produkt pausieren", risk_level="medium",
        required_role=None, requires_approval_by_default=True, idempotent=True, reversible=True,
        timeout_seconds=10, retry_allowed=True, allowed_environments=ALL_ENVIRONMENTS,
        required_inputs=("product_id",), output_schema=("previous_status", "new_status"),
        note="Setzt vt_affiliate_products.status='paused'; vorheriger Status wird für Rollback gespeichert.",
    ),
    "api_verbindung_testen": ActionDefinition(
        action_type="api_verbindung_testen", label="API-/Provider-Verbindung testen", risk_level="low",
        required_role=None, requires_approval_by_default=False, idempotent=True, reversible=False,
        timeout_seconds=15, retry_allowed=True, allowed_environments=ALL_ENVIRONMENTS,
        required_inputs=(), output_schema=("providers",),
        note="Reuses core/affiliate_provider.py::get_provider_statuses() — nur lesend, keine Mutation.",
    ),
    "health_check_ausfuehren": ActionDefinition(
        action_type="health_check_ausfuehren", label="System Health Check ausführen", risk_level="low",
        required_role=None, requires_approval_by_default=False, idempotent=True, reversible=False,
        timeout_seconds=15, retry_allowed=True, allowed_environments=ALL_ENVIRONMENTS,
        required_inputs=(), output_schema=("integration_report",),
        note="Reuses core/integrations.py::get_full_integration_report() — nur lesend.",
    ),
    "analytics_snapshot_erstellen": ActionDefinition(
        action_type="analytics_snapshot_erstellen", label="Analytics-/Business-Snapshot erstellen", risk_level="low",
        required_role=None, requires_approval_by_default=False, idempotent=True, reversible=False,
        timeout_seconds=15, retry_allowed=True, allowed_environments=ALL_ENVIRONMENTS,
        required_inputs=(), output_schema=("dashboard_snapshot",),
        note="Reuses core/founder_business_metrics.py::get_business_dashboard() — Ergebnis landet im Run-Result (kein neues Report-Modell nötig).",
    ),
    "founder_alert_erzeugen": ActionDefinition(
        action_type="founder_alert_erzeugen", label="Founder Alert erzeugen", risk_level="low",
        required_role=None, requires_approval_by_default=False, idempotent=True, reversible=False,
        timeout_seconds=10, retry_allowed=True, allowed_environments=ALL_ENVIRONMENTS,
        required_inputs=("title", "message"), output_schema=("alert_id",),
        note="Idempotent (dedupe_key) in vt_automation_alerts, dedupliziert/priorisiert (siehe Alerting-Sektion).",
    ),
    "regel_pausieren": ActionDefinition(
        action_type="regel_pausieren", label="Automatisierungsregel pausieren", risk_level="low",
        required_role=None, requires_approval_by_default=False, idempotent=True, reversible=True,
        timeout_seconds=10, retry_allowed=True, allowed_environments=ALL_ENVIRONMENTS,
        required_inputs=("target_rule_id",), output_schema=("rule_id", "new_status"),
        note="Meta-Aktion: setzt eine (i. d. R. die eigene) Regel auf status='pausiert', enabled=False.",
    ),
    "workflow_stoppen": ActionDefinition(
        action_type="workflow_stoppen", label="Laufenden Workflow stoppen", risk_level="low",
        required_role=None, requires_approval_by_default=False, idempotent=True, reversible=False,
        timeout_seconds=5, retry_allowed=False, allowed_environments=ALL_ENVIRONMENTS,
        required_inputs=("target_run_id",), output_schema=("run_id", "new_status"),
        note="Meta-Aktion: markiert einen laufenden/wartenden Run als 'abgebrochen'.",
    ),
}

# Named exactly so a founder-facing UI/README can explain the omission
# instead of silently missing them.
NOT_IMPLEMENTED_ACTIONS_NOTE = (
    "Folgende im Auftrag genannte Aktionen sind bewusst NICHT implementiert, "
    "weil keine reale Infrastruktur dahinter existiert (kein Cache-Layer, kein "
    "Changelog-System, keine Doku-Generierung, kein Support-Ticket-Kategoriefeld, "
    "kein Hintergrundjob/Queue-Runtime): Cache leeren, Dokumentation aktualisieren, "
    "Changelog vorbereiten, Support-Ticket kategorisieren, Background Job neu starten, "
    "Produktdaten neu synchronisieren (über den bestehenden manuellen Import hinaus), "
    "Integration als fehlerhaft markieren (es gibt keinen Integrationsstatus, der "
    "persistiert und nicht schon in core/integrations.py live berechnet wird)."
)

CRITICAL_ACTIONS_NOT_IMPLEMENTED_NOTE = (
    "Keine Aktion mit riskLevel='critical' ist in dieser Registry vorhanden. "
    "Preisänderungen, Tarifänderungen, rechtliche Texte, Datenschutzregeln, "
    "Kontosperrungen/-löschungen, Produktionsdeployments, Rückerstattungen, "
    "Verträge, API-Schlüssel-Rotation und Sicherheitsrichtlinien-Änderungen "
    "können über die Automation Engine niemals ausgeführt werden — unabhängig "
    "davon, was eine Regel als riskLevel angibt."
)


def get_action_definition(action_type: str) -> ActionDefinition | None:
    return ACTION_REGISTRY.get(action_type)


def is_action_allowed(action_type: str, *, environment: str) -> tuple[bool, str | None]:
    """Returns `(allowed, reason_if_blocked)`. Never raises."""
    definition = get_action_definition(action_type)
    if definition is None:
        return False, f"Unbekannte Aktion '{action_type}' — nicht in der Safe Action Registry."
    if not definition.implemented:
        return False, f"Aktion '{action_type}' ist registriert, aber nicht implementiert."
    if environment not in definition.allowed_environments:
        return False, f"Aktion '{action_type}' ist in Umgebung '{environment}' nicht erlaubt."
    return True, None


def validate_risk_level(risk_level: str) -> None:
    if risk_level not in RISK_LEVELS:
        raise ValueError(f"Ungültiges Risk Level. Erlaubt: {', '.join(sorted(RISK_LEVELS))}")
    if risk_level == "critical":
        raise ValueError(
            "Risk Level 'critical' ist für Automatisierungsregeln nicht erlaubt. " + CRITICAL_ACTIONS_NOT_IMPLEMENTED_NOTE
        )


def validate_approval_policy(policy: str) -> None:
    if policy not in APPROVAL_POLICIES:
        raise ValueError(f"Ungültige Approval Policy. Erlaubt: {', '.join(sorted(APPROVAL_POLICIES))}")


def validate_actions(actions: list[dict], *, environment: str) -> None:
    """Rejects a rule outright if it references any action not present,
    unimplemented, or disallowed in the target environment — this is the
    one function that prevents `riskLevel` tampering via an unregistered
    action slipping through, and prevents disabled/critical actions from
    ever being schedulable."""
    if not actions:
        raise ValueError("Eine Regel benötigt mindestens eine Aktion.")
    for action in actions:
        action_type = action.get("action_type")
        if not action_type:
            raise ValueError("Jede Aktion benötigt ein 'action_type'-Feld.")
        allowed, reason = is_action_allowed(action_type, environment=environment)
        if not allowed:
            raise ValueError(reason)
