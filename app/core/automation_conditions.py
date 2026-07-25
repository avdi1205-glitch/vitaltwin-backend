"""Automation Engine — Condition Evaluator (VitalTwin Enterprise, Founder
Operating System, Submodule G).

A pure, side-effect-free evaluator for combinable AND/OR condition groups
over a flat `context` dict (numbers/strings/booleans/dates-as-ISO-strings
computed by `core/automation_engine.py` right before evaluation — never a
live database handle, so this module stays fully unit-testable).

Shape of a condition node — either a group:

    {"all": [<condition>, ...]}   # AND
    {"any": [<condition>, ...]}   # OR

or a leaf:

    {"field": "broken_links_count", "operator": "greater_than", "value": 0}

Supported operators: equals, not_equals, greater_than, less_than,
contains, missing, stale, failed_count, time_window, age_in_days,
consecutive_failures, cost_threshold. The spec additionally lists
`region`/`category`/`status`/`role`/`approval_status` as condition types —
these are field *categories*, not distinct comparison logic, so they are
supported as convenience equality shortcuts (`operator` may equal the
field name itself, e.g. `{"operator": "status", "field": "status",
"value": "aktiv"}`) that fall through to the same `equals` comparison.
"""

from __future__ import annotations

from datetime import datetime, timezone

_EQUALITY_SHORTCUT_OPERATORS = {"region", "category", "status", "role", "approval_status"}


def _to_number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def evaluate_condition(condition: dict, context: dict) -> bool:
    """Recursively evaluates one condition node against `context`. Unknown
    fields, missing values, or malformed nodes evaluate to `False` rather
    than raising — a broken condition must never crash the evaluation
    loop, it should simply mean "not due"."""
    if not isinstance(condition, dict):
        return False

    if "all" in condition:
        children = condition.get("all") or []
        return bool(children) and all(evaluate_condition(c, context) for c in children)
    if "any" in condition:
        children = condition.get("any") or []
        return any(evaluate_condition(c, context) for c in children)

    field = condition.get("field")
    operator = condition.get("operator")
    expected = condition.get("value")
    if not field or not operator:
        return False

    actual = context.get(field)

    if operator in _EQUALITY_SHORTCUT_OPERATORS:
        operator = "equals"

    if operator == "missing":
        return actual is None or actual == ""
    if operator == "equals":
        return actual == expected
    if operator == "not_equals":
        return actual != expected
    if operator == "contains":
        if actual is None:
            return False
        try:
            return expected in actual  # type: ignore[operator]
        except TypeError:
            return False

    if operator in {"greater_than", "less_than", "failed_count", "consecutive_failures", "cost_threshold"}:
        actual_num = _to_number(actual)
        expected_num = _to_number(expected)
        if actual_num is None or expected_num is None:
            return False
        if operator == "less_than":
            return actual_num < expected_num
        # greater_than / failed_count / consecutive_failures / cost_threshold
        # all mean "the measured number exceeds the configured threshold".
        return actual_num > expected_num

    if operator in {"stale", "age_in_days"}:
        # `actual` is expected to be an ISO timestamp string (e.g. a
        # `created_at`/`last_run_at`); `expected` is the threshold in days.
        parsed = _parse_iso(actual)
        threshold_days = _to_number(expected)
        if parsed is None or threshold_days is None:
            return False
        age_days = (datetime.now(timezone.utc) - parsed).total_seconds() / 86400
        return age_days >= threshold_days

    if operator == "time_window":
        # `expected` = {"start_hour": int, "end_hour": int} in the
        # founder's local sense; `actual` = current hour (int, 0-23) —
        # computed by the caller using the founder's configured timezone.
        if not isinstance(expected, dict) or actual is None:
            return False
        start_hour = expected.get("start_hour")
        end_hour = expected.get("end_hour")
        try:
            hour = int(actual)
        except (TypeError, ValueError):
            return False
        if start_hour is None or end_hour is None:
            return False
        if start_hour <= end_hour:
            return start_hour <= hour <= end_hour
        return hour >= start_hour or hour <= end_hour  # wraps past midnight

    return False


def evaluate_conditions(conditions: list[dict] | dict | None, context: dict) -> bool:
    """Top-level entry point. An empty/missing condition list means
    "always true" (the trigger alone decides), matching every other
    detector in this codebase where a rule with no extra condition simply
    fires whenever its trigger fires."""
    if not conditions:
        return True
    if isinstance(conditions, dict):
        return evaluate_condition(conditions, context)
    return all(evaluate_condition(c, context) for c in conditions)
