"""Lifestyle-Simulationen ("Wellness-Szenarien", Pro/Family).

Constitution rule 10 requires simulations to be presented ONLY as
simulations, never as medical predictions. Constitution rule 12 ("KI ist
nicht die Datenquelle") requires calculation first, no fabricated
correlation/causation between metrics (rule 5: "keine Kausalität aus
bloßen Zusammenhängen ableiten").

This module therefore does exactly one thing: it reuses the SAME
`services/trends.py::compute_trend` every trend/baseline endpoint already
uses to get the user's REAL current average for one field, adds a
user-supplied hypothetical delta, and clamps the result to that field's own
already-enforced valid range (see `core/validation.py`). No AI call, no
new statistics engine, no claim about any OTHER metric changing as a
result — purely "if your own average for X had been Y instead"."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ..core.validation import MAX_MOVEMENT_MINUTES, MAX_SLEEP_HOURS, SCALE_MAX, SCALE_MIN
from .trends import compute_trend

# Same 3 fields the Personal Baseline Engine already supports with real
# stored data (see services/personal_baseline.py docstring) — no fields are
# added here that lack real underlying data.
FIELD_BOUNDS: dict[str, tuple[float, float]] = {
    "sleep_hours": (0.0, MAX_SLEEP_HOURS),
    "movement_minutes": (0.0, float(MAX_MOVEMENT_MINUTES)),
    "stress": (float(SCALE_MIN), float(SCALE_MAX)),
}

SIMULATABLE_FIELDS = tuple(FIELD_BOUNDS.keys())

SIMULATION_DISCLAIMER = (
    "Dies ist eine rein rechnerische Simulation deines eigenen Durchschnittswerts auf Grundlage "
    "deiner bisherigen Eintragungen — keine medizinische Vorhersage, keine Diagnose und keine "
    "Garantie für ein reales Ergebnis."
)


@dataclass(frozen=True)
class SimulationResult:
    field: str
    window_days: int
    current_average: float | None
    delta: float
    simulated_average: float | None
    data_points: int
    data_quality: str
    disclaimer: str


def simulate_metric_change(
    entries: list[dict[str, object]], *, field: str, delta: float, today: date, window_days: int = 7
) -> SimulationResult:
    """Recomputes the user's real `window_days` average for `field`, adds
    `delta`, and clamps to the field's real valid range. Returns
    `simulated_average=None` (never a fabricated number) if there isn't
    even a current average to project from ("Noch nicht genügend Daten")."""
    trend = compute_trend(entries, field=field, window_days=window_days, today=today)
    simulated_average = None
    if trend.average is not None:
        low, high = FIELD_BOUNDS[field]
        simulated_average = min(high, max(low, trend.average + delta))
    return SimulationResult(
        field=field,
        window_days=window_days,
        current_average=trend.average,
        delta=delta,
        simulated_average=simulated_average,
        data_points=trend.data_points,
        data_quality=trend.data_quality,
        disclaimer=SIMULATION_DISCLAIMER,
    )
