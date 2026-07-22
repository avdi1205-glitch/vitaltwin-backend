"""Unit tests for the Etappe 4 Pydantic validation models in
`app.routers.recommendations` (Decision, Outcome, Feedback). Pure model
construction — no network/database access."""

import pytest
from pydantic import ValidationError

from app.routers.recommendations import DecisionInput, FeedbackInput, OutcomeInput


class TestDecisionInput:
    @pytest.mark.parametrize("decision", ["accepted", "modified", "skipped", "rejected"])
    def test_accepts_all_valid_decisions(self, decision):
        assert DecisionInput(decision=decision).decision == decision

    def test_rejects_invalid_decision(self):
        with pytest.raises(ValidationError):
            DecisionInput(decision="not-a-decision")

    def test_reason_too_long_is_rejected(self):
        with pytest.raises(ValidationError):
            DecisionInput(decision="rejected", reason="x" * 281)

    def test_modified_action_is_stripped(self):
        model = DecisionInput(decision="modified", modified_action="  andere Aktion  ")
        assert model.modified_action == "andere Aktion"


class TestOutcomeInput:
    @pytest.mark.parametrize(
        "status", ["not_started", "started", "partially_completed", "completed", "not_implemented"]
    )
    def test_accepts_all_valid_statuses(self, status):
        assert OutcomeInput(outcome_status=status).outcome_status == status

    def test_rejects_invalid_status(self):
        with pytest.raises(ValidationError):
            OutcomeInput(outcome_status="not-a-status")

    @pytest.mark.parametrize(
        "source", ["user_reported", "derived_from_checkin", "derived_from_habit_entry", "imported_from_wearable"]
    )
    def test_accepts_all_valid_sources(self, source):
        assert OutcomeInput(outcome_status="completed", outcome_source=source).outcome_source == source

    def test_rejects_invalid_source(self):
        with pytest.raises(ValidationError):
            OutcomeInput(outcome_status="completed", outcome_source="not-a-source")

    def test_default_source_is_user_reported(self):
        assert OutcomeInput(outcome_status="started").outcome_source == "user_reported"

    def test_notes_too_long_is_rejected(self):
        with pytest.raises(ValidationError):
            OutcomeInput(outcome_status="started", result_notes="x" * 501)


class TestFeedbackInput:
    @pytest.mark.parametrize("helpfulness", ["helpful", "partially_helpful", "not_helpful"])
    def test_accepts_all_valid_helpfulness_values(self, helpfulness):
        assert FeedbackInput(helpfulness=helpfulness).helpfulness == helpfulness

    def test_rejects_invalid_helpfulness(self):
        with pytest.raises(ValidationError):
            FeedbackInput(helpfulness="sehr gut")

    @pytest.mark.parametrize(
        "reason",
        [
            "nicht_passend",
            "falscher_zeitpunkt",
            "zu_schwierig",
            "zu_einfach",
            "bereits_erledigt",
            "unverstaendlich",
            "nicht_relevant",
            "anderer_grund",
        ],
    )
    def test_accepts_all_valid_reasons(self, reason):
        assert FeedbackInput(helpfulness="not_helpful", reason=reason).reason == reason

    def test_rejects_invalid_reason(self):
        with pytest.raises(ValidationError):
            FeedbackInput(helpfulness="not_helpful", reason="random-reason")

    def test_comment_too_long_is_rejected(self):
        with pytest.raises(ValidationError):
            FeedbackInput(helpfulness="helpful", comment="x" * 501)

    def test_comment_within_limit_is_accepted(self):
        model = FeedbackInput(helpfulness="helpful", comment="x" * 500)
        assert model.comment == "x" * 500
