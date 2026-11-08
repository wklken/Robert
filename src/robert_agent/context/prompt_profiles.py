"""Fixed stage and guidance selection for the prompt renderer."""

from dataclasses import dataclass

from robert_agent.foundation.contracts.delivery import OutputType


@dataclass(frozen=True, slots=True)
class StageProfile:
    transport: str
    schemas: tuple[str, ...]
    tools: bool = True
    boundary: str = ""


STAGES = {
    "task.classify": StageProfile("helper", (OutputType.CLASSIFICATION_RESULT,)),
    "task.implement": StageProfile("helper", (OutputType.NEW_PR,)),
    "task.update_pr": StageProfile("helper", (OutputType.UPDATE_EXISTING_PR,)),
    "task.analyze": StageProfile("helper", (OutputType.LOCAL_RESULT, OutputType.COMMENT_ANALYSIS)),
    "task.review_pr": StageProfile("helper", (OutputType.PR_REVIEW_COMMENT,)),
    "task.review_feedback": StageProfile("helper", (OutputType.REVIEW_COMMENT,)),
    "task.ask_user": StageProfile("helper", (OutputType.WAITING_FOR_USER,)),
    "task.remediate_ci": StageProfile("helper", (OutputType.UPDATE_EXISTING_PR,)),
    "task.resolve_conflict": StageProfile("helper", (OutputType.UPDATE_EXISTING_PR,)),
    "maintenance.repair": StageProfile("stdout-json", (OutputType.NEW_PR,)),
    "review.prepare": StageProfile("atomic-files", ("review_context",), boundary="review"),
    "review.evaluate": StageProfile("atomic-files", ("review_result",), boundary="review"),
    "review.synthesize": StageProfile("atomic-files", ("review_evaluation",), boundary="review"),
    "vote.prepare": StageProfile("atomic-files", ("prepared_request",), boundary="vote"),
    "vote.cast": StageProfile("atomic-files", ("ballot",), boundary="vote"),
    "vote.synthesize": StageProfile("atomic-files", ("synthesis",), boundary="vote"),
    "supervision.diagnose": StageProfile("stdout-json", ("diagnosis",), tools=False),
    "supervision.decide": StageProfile("stdout-json", ("decision",), tools=False),
}


# A guidance ID selects one named fragment in one packaged resource and places
# it in one section. A caller cannot supply either a path or a prose fragment.
GUIDANCE = {
    name: (resource, section)
    for resource, section, names in (
        ("issue-dispatch", "Work", ("issue-dispatch",)),
        ("worker", "Boundaries", ("worker-boundaries",)),
        ("worker", "Work", ("collaboration", "resume", "skills", "memory", "knowledge")),
        ("worker", "Verification", ("unit-tests", "verification")),
        ("worker", "Delivery", ("memory-delta", "memory-used", "remediation-evidence")),
        (
            "feedback",
            "Work",
            (
                "feedback-classify",
                "feedback-required",
                "feedback-common",
                "feedback-comment",
                "feedback-update",
                "feedback-mixed",
                "feedback-policy",
            ),
        ),
        ("publication", "Delivery", ("publication", "targets", "comment", "push", "open", "update")),
        ("runtime", "Boundaries", ("runtime", "snapshot", "heartbeat")),
    )
    for name in names
}
