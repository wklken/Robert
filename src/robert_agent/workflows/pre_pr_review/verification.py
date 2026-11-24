"""Required final gates without rewriting historical worker evidence."""

from robert_agent.foundation.contracts.delivery import VerificationStatus


def final_verification_checks(checks):
    """Return all evidence unchanged after checking the final required gates.

    The original submission contract must still validate every entry, including
    optional failures. The caller pins final_sha to the clean writer HEAD; this
    policy does not infer a commit or obsolescence from a check's purpose.
    """
    if not isinstance(checks, list):
        raise ValueError("final verification requires a list of checks")
    required = [check for check in checks if isinstance(check, dict) and check.get("required") is True]
    if not required:
        raise ValueError("final verification requires at least one required check")
    if any(check.get("status") != VerificationStatus.PASSED.value for check in required):
        raise ValueError("final verification requires every required check to pass")
    return checks
