"""Pure output policies shared by submission and final delivery audit."""

from robert_agent.foundation.contracts.delivery import QuestionKind, ReviewAction, ReviewVerdict, VerificationStatus

VALID_OPERATOR_QUESTION_KINDS = frozenset(kind.value for kind in QuestionKind)

VALID_REVIEW_VERDICTS = frozenset(verdict.value for verdict in ReviewVerdict)

VALID_REVIEW_ACTIONS = frozenset(action.value for action in ReviewAction)

IMPLEMENTABLE_VERDICTS = frozenset({ReviewVerdict.CORRECT.value, ReviewVerdict.PARTIALLY_CORRECT.value})


VALID_VERIFICATION_STATUSES = frozenset(status.value for status in VerificationStatus)


def validate_operator_question(result):
    question = result.get("operator_question")
    if not isinstance(question, dict):
        return "waiting_for_user requires operator_question"
    if question.get("kind") not in VALID_OPERATOR_QUESTION_KINDS:
        return "operator_question.kind is invalid"
    summary = question.get("summary")
    if not isinstance(summary, str) or not summary.strip() or len(summary) > 2000:
        return "operator_question.summary is invalid"
    choices = question.get("choices", [])
    if not isinstance(choices, list) or len(choices) > 5:
        return "operator_question.choices is invalid"
    for choice in choices:
        if not isinstance(choice, dict):
            return "operator_question choices must be objects"
        if not isinstance(choice.get("id"), str) or not choice["id"]:
            return "operator_question choice id is required"
        if not isinstance(choice.get("label"), str) or not choice["label"]:
            return "operator_question choice label is required"
    return None
