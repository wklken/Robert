"""Stable delivery values; member values are existing storage and protocol strings."""

from enum import StrEnum, unique


@unique
class GitHubActionType(StrEnum):
    COMMENT = "comment"
    PUSH_EXISTING_PR = "push_existing_pr"
    OPEN_PR = "open_pr"
    SUBMIT_PR_REVIEW = "submit_pr_review"


@unique
class OutputType(StrEnum):
    CLASSIFICATION_RESULT = "classification_result"
    LOCAL_RESULT = "local_result"
    COMMENT_ANALYSIS = "comment_analysis"
    NEW_PR = "new_pr"
    UPDATE_EXISTING_PR = "update_existing_pr"
    REVIEW_COMMENT = "review_comment"
    PR_REVIEW_COMMENT = "pr_review_comment"
    WAITING_FOR_USER = "waiting_for_user"
    MULTI_AGENT_REVIEW_REPORT = "multi_agent_review_report"
    MULTI_AGENT_VOTE_REPORT = "multi_agent_vote_report"


@unique
class AuditStatus(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    POLICY_VIOLATION = "policy_violation"
    FAILED = "failed"


@unique
class PublishStatus(StrEnum):
    NOT_PUBLISHED = "not_published"
    PUBLISHED = "published"
    SKIPPED = "skipped"


@unique
class PublicationStatus(StrEnum):
    PREPARED = "prepared"
    EXECUTING = "executing"
    PUBLISHED = "published"
    CONFIRMED_ABSENT = "confirmed_absent"
    FAILED = "failed"
    ATTENTION = "attention"


@unique
class ReviewVerdict(StrEnum):
    CORRECT = "correct"
    PARTIALLY_CORRECT = "partially_correct"
    INCORRECT = "incorrect"
    UNVERIFIED = "unverified"


@unique
class ReviewAction(StrEnum):
    IMPLEMENT = "implement"
    SKIP = "skip"
    COMMENT = "comment"
    CLARIFY = "clarify"


@unique
class VerificationStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"


@unique
class QuestionKind(StrEnum):
    CLARIFICATION = "clarification"
    SCOPE_DECISION = "scope_decision"
    COMPLETION_ACCEPTANCE = "completion_acceptance"
