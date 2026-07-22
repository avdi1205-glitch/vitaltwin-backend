"""Unit tests for `app.services.explainability` (Etappe 4 §7).

Pure functions — no database access.
"""

from app.services.explainability import build_explanation_response


class TestBuildExplanationResponse:
    def test_extracts_stored_explanation_fields(self):
        recommendation = {
            "confidence": 0.8,
            "goal_id": None,
            "habit_id": "habit-1",
            "explanation": {
                "rule_name": "repeated_short_sleep",
                "data_used": ["sleep_hours"],
                "period_days": 7,
                "data_points": 5,
                "data_quality": "calculated",
                "expected_benefit": "Bessere Schlafqualität.",
            },
        }
        result = build_explanation_response(recommendation)
        assert result["rule_name"] == "repeated_short_sleep"
        assert result["data_used"] == ["sleep_hours"]
        assert result["period_days"] == 7
        assert result["data_points"] == 5
        assert result["confidence"] == 0.8
        assert result["habit_id"] == "habit-1"
        assert result["type"] == "allgemeine Regel"
        assert "keine medizinische Notwendigkeit" in result["disclaimer"]

    def test_missing_explanation_does_not_crash(self):
        result = build_explanation_response({"confidence": None})
        assert result["rule_name"] is None
        assert result["data_used"] == []
        assert result["type"] == "unbekannt"

    def test_never_fabricates_a_rule_name(self):
        result = build_explanation_response({"explanation": {}})
        assert result["rule_name"] is None
        assert result["type"] == "unbekannt"
