"""Executable inventory for the offline lifecycle suites."""

from dataclasses import dataclass
from functools import partial
from typing import Callable

import scenarios_delivery as delivery
import scenarios_execution as execution
import scenarios_intake as intake
import scenarios_messages as messages
import scenarios_native as native
import scenarios_remediation as remediation
import scenarios_operator as operator
import scenarios_runtime as runtime
import scenarios_workflows as workflows
import scenarios_supervision as supervision


@dataclass(frozen=True)
class Case:
    id: str
    description: str
    run: Callable
    timeout: int = 180
    suite: str = "core"
    smoke: bool = False
    resume: Callable | None = None


CASES = (
    Case(
        "O04.01.provider",
        "Chat rejects the wrong provider",
        partial(messages.refused, mode="provider"),
        suite="extended",
    ),
    Case(
        "O04.01.session", "Chat rejects the wrong session", partial(messages.refused, mode="session"), suite="extended"
    ),
    Case("O04.01.sender", "Chat rejects the wrong sender", partial(messages.refused, mode="sender"), suite="extended"),
    Case(
        "O04.01.repo",
        "Authenticated chat rejects an out-of-scope repository",
        partial(messages.refused, mode="repo"),
        suite="extended",
    ),
    Case("O04.02.reply_replay", "Duplicate chat reply returns one durable receipt", messages.replay, suite="extended"),
    Case(
        "O04.02.old_question", "An old chat question cannot answer a newer one", messages.old_question, suite="extended"
    ),
    Case(
        "O04.02.ack_only",
        "Chat acknowledges an incident without resolving question or process",
        messages.ack_only,
        240,
        suite="extended",
    ),
    Case(
        "N05.01", "Merge conflict repair precedes allowed CI failure", remediation.conflict_first, 300, suite="extended"
    ),
    Case("N05.02", "The CI repair resolves after a matching green check", remediation.ci_fix, 300, suite="extended"),
    Case(
        "N05.03.unapproved_check",
        "A failing check outside the allowlist cannot trigger repair",
        remediation.denied,
        suite="extended",
    ),
    Case(
        "N05.03.third_party_pr",
        "A non-Robert PR cannot trigger automatic repair",
        partial(remediation.denied, foreign=True),
        suite="extended",
    ),
    Case(
        "N06.01",
        "Failed same-head remediation consumes its configured episode budget",
        remediation.exhausted,
        600,
        suite="slow",
    ),
    Case(
        "N01.01",
        "Eligible PR author runs two native reviewers and posts a review",
        native.review,
        240,
        suite="extended",
    ),
    Case(
        "N01.02",
        "Non-eligible PR author uses the ordinary review worker",
        partial(native.review, eligible=False),
        180,
        suite="extended",
    ),
    Case("N02.01", "Review and vote share two actual model slots", native.shared_slots, 240, suite="extended"),
    Case(
        "N02.02",
        "Recovering a bad reviewer output preserves the successful sibling",
        native.review_retry,
        240,
        suite="extended",
    ),
    Case("N02.03", "Recovering bad synthesis preserves the sealed ballots", native.vote_retry, 240, suite="extended"),
    Case(
        "N03.01",
        "Trusted clarification resumes the vote round with a strict majority",
        native.clarified_vote,
        240,
        suite="extended",
    ),
    Case("N03.02", "Two different votes and one abstention have no winner", native.tied_vote, 180, suite="extended"),
    Case("N03.03", "A failed voter cannot count as an abstention", native.failed_vote, 180, suite="extended"),
    Case(
        "N04.01",
        "Web cancellation stops all registered review model groups",
        native.cancel_review,
        240,
        suite="extended",
    ),
    Case("N04.02", "Moved PR head supersedes an already prepared review", native.stale_review, 240, suite="extended"),
    Case("N04.03", "Deleted adopted source supersedes a prepared vote", native.stale_vote, 180, suite="extended"),
    Case("I01.01", "Repeated mention and notification produce one comment", intake.discover_once, 120, smoke=True),
    Case("I01.02", "Repeated assignment produces one result", partial(intake.discover_once, assigned=True), 120),
    Case("I02.01", "Untrusted issue commenter", partial(intake.unauthorized, mode="outsider"), 90),
    Case("I02.02", "Untrusted editor of a trusted comment", partial(intake.unauthorized, mode="editor"), 90),
    Case(
        "I02.03",
        "Edited comment whose editor cannot be retrieved",
        partial(intake.unauthorized, mode="missing_editor"),
        90,
    ),
    Case("I03.01", "Complete notification snapshot survives 304", intake.notification_pages, suite="extended"),
    Case(
        "I03.02",
        "Incomplete second page cannot advance notification cursor",
        partial(intake.notification_pages, failure=True),
        suite="extended",
    ),
    Case("I04.01", "Source failure is isolated to its repository", intake.source_isolation, suite="extended"),
    Case("I04.02", "Low search quota does not prevent core delivery", intake.low_search, suite="extended"),
    Case("I06.01", "Pending observation uses current trust on retry", intake.pending_reauthorization, 300, "extended"),
    Case(
        "I07.01",
        "Exhausted preparation batch is retained for explicit retry",
        partial(intake.pending_reauthorization, exhaust=True),
        900,
        "slow",
    ),
    Case("I05.01", "Authority is revoked before publication", delivery.withdraw, 150),
    Case(
        "I05.02",
        "Revocation permits read-only confirmation of the existing effect",
        partial(delivery.withdraw, effect="present"),
    ),
    Case("I05.03", "Revocation prevents retry after an absent effect", partial(delivery.withdraw, effect="absent")),
    Case("W01.01", "Issue to real Git push and PR", delivery.new_pr, smoke=True),
    Case("W02.01", "Classifier hands off to comment analysis", workflows.classify, suite="extended"),
    Case("W05.01", "Followup updates the existing PR and merge moves the board", workflows.update_pr, 240, "extended"),
    Case("W06.01", "Closing an issue stops its current Worker", workflows.close_source, suite="extended"),
    Case(
        "W06.02",
        "Closing an unmerged PR stops its current Worker",
        partial(workflows.close_source, pull=True),
        suite="extended",
    ),
    Case(
        "W06.03",
        "Merging a PR stops its current Worker",
        partial(workflows.close_source, pull=True, merged=True),
        suite="extended",
    ),
    Case(
        "P05.01",
        "Divergent remote head cannot be overwritten",
        partial(workflows.update_pr, conflict="head"),
        suite="extended",
    ),
    Case(
        "P05.02.author",
        "Third-party PR author blocks push",
        partial(workflows.update_pr, conflict="author"),
        suite="extended",
    ),
    Case(
        "P05.02.head_repo",
        "Changed PR head repo blocks push",
        partial(workflows.update_pr, conflict="head_repo"),
        suite="extended",
    ),
    Case(
        "P05.03",
        "Changed push remote cannot escape the configured scope",
        partial(workflows.update_pr, conflict="push_url"),
        suite="extended",
    ),
    Case("W03.01", "Trusted reply creates one child", workflows.waiting),
    Case("W03.02", "Untrusted reply preserves the waiting question", partial(workflows.waiting, outsider=True)),
    Case("W03.03", "Old reply cannot answer a new question", partial(workflows.waiting, replay=True), 240),
    Case("E01.01", "Recorded result must await Worker process exit", execution.hold_receipt, smoke=True),
    Case("E02.01", "Dirty worktree resumes once with new attempt", execution.recover_dirty),
    Case("E02.02", "Recovery budget survives daemon restart", partial(execution.recover_dirty, exhaust=True), 240),
    Case("E05.01", "Canceled Worker cannot replay its result", execution.cancel, 150),
    Case(
        "E05.02",
        "Superseded Worker cannot replace new attempt result",
        partial(execution.recover_dirty, reject_stale=True),
    ),
    Case("E06.01", "Live descendant retains unknown execution and capacity", execution.unknown_descendant, 150),
    Case(
        "E06.02",
        "Cancellation stops the verified group and descendants",
        partial(execution.cancel, descendant=True),
        150,
    ),
    Case(
        "E03.01",
        "Heartbeat cannot bypass Worker hard timeout",
        partial(execution.worker_timeout, heartbeat=True),
        suite="extended",
    ),
    Case("E03.02", "A stale live Worker is eventually stopped", execution.worker_timeout, suite="extended"),
    Case("E04.02", "Invalid JSON result creates no delivery", execution.bad_json, suite="extended"),
    Case("E04.03", "Non-executable Worker releases dispatch ownership", execution.cannot_exec, suite="extended"),
    Case(
        "E07.01",
        "Identical result is idempotent and conflicting result rejected",
        execution.same_result,
        suite="extended",
    ),
    Case("E04.01", "Worker exits zero without a result", execution.no_result, suite="extended"),
    Case("P01.01", "Route rejects forbidden action", partial(delivery.rejected, mutation="action"), 90),
    Case(
        "P01.02",
        "Result cannot redirect publication to another repo",
        partial(delivery.rejected, mutation="target"),
        90,
    ),
    Case(
        "P01.03",
        "Failed verification rejects code delivery",
        partial(delivery.rejected, mutation="verification", coding=True),
        120,
    ),
    Case(
        "P01.04", "Dirty worktree rejects code delivery", partial(delivery.rejected, mutation="dirty", coding=True), 120
    ),
    Case("P02.01", "Comment effect committed before publisher group loss", delivery.lost_comment, smoke=True),
    Case("P02.02", "PR effect committed before publisher group loss", delivery.lost_pr, 210),
    Case("P03.01", "Git ref updated before publisher group loss", delivery.lost_push, 210),
    Case("P04.01", "Confirmed absent write retries once", delivery.confirmed_absent),
    Case(
        "P04.02", "Unreadable reconciliation stays in attention", partial(delivery.unknown_effect, variant="unreadable")
    ),
    Case(
        "P04.03",
        "A matching marker from another author is insufficient",
        partial(delivery.unknown_effect, variant="author"),
    ),
    Case(
        "P04.04",
        "A matching marker with changed content is insufficient",
        partial(delivery.unknown_effect, variant="body"),
    ),
    Case(
        "P07.01",
        "Transient write failure retries",
        partial(delivery.transient_write, http_status=503),
        suite="extended",
    ),
    Case("P07.02", "Rate limited write retries", partial(delivery.transient_write, http_status=429), suite="extended"),
    Case(
        "P07.03.http401",
        "Unauthenticated write stops",
        partial(delivery.transient_write, http_status=401),
        suite="extended",
    ),
    Case(
        "P07.03.http403", "Forbidden write stops", partial(delivery.transient_write, http_status=403), suite="extended"
    ),
    Case("D01.01", "SQLite writer lock defers and replays the real Worker result", execution.deferred, 150),
    Case("D01.02", "Deferred result survives daemon crash", partial(execution.deferred, restart=True), 360),
    Case("D02.01", "New daemon reuses running Worker after lease expiry", runtime.daemon_restart, 360),
    Case("D02.02", "New daemon cleans recorded scanner group", partial(runtime.daemon_restart, scanner=True), 360),
    Case(
        "D06.01",
        "Graceful container recreation preserves completed work",
        runtime.recreate,
        240,
        "extended",
        resume=runtime.resume_recreated,
    ),
    Case(
        "D06.02",
        "Abrupt container recreation preserves storage and permits new work",
        partial(runtime.recreate, crash=True),
        360,
        "extended",
        resume=runtime.resume_recreated,
    ),
    Case("O06.01", "SIGTERM stops the running service stack", runtime.terminate_stack, 240, "slow"),
    Case("D03.01", "Blocked scanner leaves local delivery runnable", runtime.dual_lane, suite="extended"),
    Case(
        "D03.02",
        "Blocked publication leaves another task runnable",
        partial(runtime.dual_lane, publisher=True),
        suite="extended",
    ),
    Case("D04.01", "Real worker process peak honors global and repository limits", runtime.capacity, suite="extended"),
    Case(
        "D05.01", "Paused local drain records completed Worker without publishing", runtime.pause_local, 240, "extended"
    ),
    Case(
        "D05.02",
        "Paused local drain stops expired Worker without recovery",
        partial(runtime.pause_local, timeout=True),
        240,
        "extended",
    ),
    Case("D04.02", "Second daemon cannot acquire the active lease", runtime.daemon_competition, suite="extended"),
    Case("S01.01", "Observer stays independent during daemon failure", supervision.observing, 240, "extended"),
    Case(
        "S02.01", "Authorized spool replay records and verifies a receipt", supervision.spool_recovery, 240, "extended"
    ),
    Case(
        "S02.02",
        "Spool outside recovery scope replays without a grant",
        partial(supervision.spool_recovery, authorized=False),
        240,
        "extended",
    ),
    Case("D07.01", "Observer recovers from a real SQLite write outage", supervision.outage, 240, "slow"),
    Case("S04.02", "A fabricated candidate cannot gain a grant", supervision.invalid_candidate, suite="extended"),
    Case("S05.01", "Persistent question accepts only the current decision ID", supervision.ask_user, 240, "extended"),
    Case("S05.02", "Repeated notice has a stable delivery identity", supervision.notify, suite="extended"),
    Case("S05.03.incomplete", "Supervision cannot finish an incomplete goal", supervision.finish, suite="extended"),
    Case(
        "S05.03.complete",
        "Completed goal can end supervision",
        partial(supervision.finish, completed_task=True),
        suite="extended",
    ),
    Case("S06.01", "Provider retry window survives observer restart", supervision.rate_limited, 360, "slow"),
    Case("O02.01", "Board follows running and pending publication facts", operator.board_progress, 300, "extended"),
    Case(
        "O03.01",
        "Notification retries preserve receiver idempotency",
        partial(operator.notification, transport=True),
        suite="extended",
    ),
    Case("O03.02", "Unconfigured notification remains undelivered", operator.notification, suite="extended"),
    Case("O01.01", "Web idempotency survives Web restart", operator.idempotency, 150),
    Case("O01.02", "Reply replay survives Web restart", operator.reply_replay),
    Case("O01.03", "Retry replay has one new execution", operator.retry_replay),
    Case("O01.04", "Cancel replay terminates a local Worker once", operator.cancel_replay, 150),
    Case("O01.09", "An obsolete question ID cannot accept a reply", partial(operator.reply_replay, stale=True), 150),
    Case("O01.10", "Read-only Web refuses a valid write", operator.read_only, 60),
    Case("O01.05", "Stale version cannot edit current work", operator.version_conflict, 60),
    Case("O01.07.origin", "Invalid request origin", partial(operator.refused, mode="origin"), 60),
    Case("O01.07.host", "Invalid request host", partial(operator.refused, mode="host"), 60),
    Case("O01.08.missing_key", "Missing idempotency key", partial(operator.refused, mode="key"), 60),
    Case("O01.08.invalid_json", "Malformed JSON payload", partial(operator.refused, mode="json"), 60),
    Case("O01.06", "Web refuses a write without CSRF", operator.csrf, 60, smoke=True),
    Case("P06.01", "Explicit retry reuses a failed push result", workflows.failed_push_retry, 240, "extended"),
    Case("P07.04", "Transient publication exhausts its persisted retry budget", delivery.budget, 240, "extended"),
    Case("P08.01", "Dry-run has no GitHub or Git effects", delivery.dry_run, suite="extended"),
    Case("P08.02", "Public result drops a synthetic credential", delivery.redaction, suite="extended"),
    Case("W02.02", "Trusted request on third-party PR stays comment-only", workflows.third_party, suite="extended"),
    Case("W02.03", "Selected named worker launches its actual command", workflows.named_worker, suite="extended"),
    Case(
        "W04.01", "Pending source event survives daemon restart and hands off once", workflows.handoff, 360, "extended"
    ),
    Case("E07.02", "Damaged result spool does not block a valid result", execution.corrupt_spool, suite="extended"),
    Case(
        "S03.01",
        "Wait defers an offered CI action until a new decision permits it",
        supervision.remediation_decision,
        300,
        "extended",
    ),
    Case(
        "S04.01",
        "New head invalidates a held decision bundle",
        partial(supervision.remediation_decision, stale=True),
        300,
        "extended",
    ),
)
