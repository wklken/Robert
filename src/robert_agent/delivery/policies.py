"""Pure output policies shared by submission and final delivery audit."""

from robert_agent.foundation.question_choices import validation_error as choice_validation_error
from robert_agent.foundation.contracts.delivery import QuestionKind, ReviewAction, ReviewVerdict, VerificationStatus

VALID_OPERATOR_QUESTION_KINDS = frozenset(kind.value for kind in QuestionKind)

VALID_REVIEW_VERDICTS = frozenset(verdict.value for verdict in ReviewVerdict)

VALID_REVIEW_ACTIONS = frozenset(action.value for action in ReviewAction)

IMPLEMENTABLE_VERDICTS = frozenset({ReviewVerdict.CORRECT.value, ReviewVerdict.PARTIALLY_CORRECT.value})


VALID_VERIFICATION_STATUSES = frozenset(status.value for status in VerificationStatus)


def validate_operator_question(result, *, require_choices=False):
    question = result.get("operator_question")
    if not isinstance(question, dict):
        return "waiting_for_user requires operator_question"
    if question.get("kind") not in VALID_OPERATOR_QUESTION_KINDS:
        return "operator_question.kind is invalid"
    summary = question.get("summary")
    if not isinstance(summary, str) or not summary.strip() or len(summary) > 2000:
        return "operator_question.summary is invalid"
    return choice_validation_error(question.get("choices", []), required=require_choices)
