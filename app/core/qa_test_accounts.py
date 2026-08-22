"""The one, project-wide QA-test-account marker (Admin Control Center §QA
Cleanup, reused by the beta discount program's public/personal-facing
counters and admin list) — deliberately NOT "email contains 'test'" (far
too broad, would risk matching a real user). Both the email prefix AND the
full_name marker must match, per the explicit double-safety requirement.
"""

from __future__ import annotations

QA_TEST_EMAIL_PREFIX = "qa-test-"
QA_TEST_NAME_MARKER = "QA TEST ACCOUNT"


def is_qa_test_account(email: str, full_name: str | None) -> bool:
    return bool(email) and email.lower().startswith(QA_TEST_EMAIL_PREFIX) and QA_TEST_NAME_MARKER in (full_name or "")
