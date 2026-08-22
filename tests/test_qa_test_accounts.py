"""Unit tests for the shared QA-test-account marker (used by both the
Admin QA-cleanup tool and the beta discount program's public/personal
counters)."""

from __future__ import annotations

from app.core.qa_test_accounts import is_qa_test_account


class TestIsQaTestAccount:
    def test_matches_when_both_prefix_and_name_marker_present(self):
        assert is_qa_test_account("qa-test-screenshot-demo@example.com", "QA TEST ACCOUNT Screenshot Demo") is True

    def test_does_not_match_without_the_name_marker(self):
        assert is_qa_test_account("qa-test-screenshot-demo@example.com", "Just A Name") is False

    def test_does_not_match_without_the_email_prefix(self):
        assert is_qa_test_account("real-user@example.com", "QA TEST ACCOUNT Something") is False

    def test_case_insensitive_on_email_prefix(self):
        assert is_qa_test_account("QA-TEST-demo@example.com", "QA TEST ACCOUNT Demo") is True

    def test_none_full_name_is_safe(self):
        assert is_qa_test_account("qa-test-demo@example.com", None) is False

    def test_empty_email_is_safe(self):
        assert is_qa_test_account("", "QA TEST ACCOUNT Demo") is False
