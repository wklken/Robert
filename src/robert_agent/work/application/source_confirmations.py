"""Observe and claim exact controller question choices without scheduling work."""

from dataclasses import asdict, dataclass

from robert_agent.delivery.tapd_policy import prepare_comment
from robert_agent.foundation.contracts.work import SourceAuthorizationType, SourceCapability
from robert_agent.foundation.timestamps import utc_timestamp
from robert_agent.persistence.queries import source_confirmation as read, source_members, source_results, source_tasks
from robert_agent.persistence.rounds.source_evidence import json_object
from robert_agent.persistence.stores import source_confirmation as write
from robert_agent.work import source_confirmation as policy, tapd_authority


@dataclass(frozen=True, slots=True)
class ConfirmationReceipt:
    question_id: str
    version: int
    scope_digest: str
    option: str
    event_id: str
    publication_id: str
    comment_id: str
    consumer_id: str | None


def scope_for_question(question):
    scope = json_object(question["scope_json"])
    return scope if scope and tapd_authority.digest(scope) == question["scope_digest"] else None


def _publication(conn, question, ticket, scope):
    root = source_results.for_source(conn, ticket.source_id, question["source_result_id"])
    source = source_results.tapd_source(conn, ticket.source_id)
    if (
        root is None
        or source is None
        or root["kind"] != "control_notice"
        or json_object(root["evidence_json"])
        != {
            "confirmation_question_id": question["question_id"],
            "confirmation_version": question["version"],
            "confirmation_scope_digest": question["scope_digest"],
        }
    ):
        return None, None
    actions = source_results.required_actions(conn, root["source_result_id"])
    if len(actions) != 1:
        return None, None
    action = actions[0]
    target = source_results.source_target(source)
    if (
        action["action_id"] != question["action_id"]
        or action["source_result_id"] != root["source_result_id"]
        or action["provider"] != "tapd"
        or action["action_type"] != "comment"
        or json_object(action["target_json"]) != target
        or (json_object(action["metadata_json"]) or {}).get("body")
        != policy.question_body(question["question_id"], question["version"], scope)
    ):
        return None, None
    confirmed = source_results.confirmed_required_effects(conn, root["source_result_id"], target)
    if not confirmed:
        return action, None
    prepared = json_object(action["prepared_evidence_json"]) or {}
    try:
        rendered = prepare_comment(
            policy.question_body(question["question_id"], question["version"], scope),
            prepared["receipt_id"],
            bot_name=scope["bot_name"],
            reply_name=scope["reply_name"],
        )
    except (KeyError, ValueError):
        return action, None
    if any(
        prepared.get(key) != getattr(rendered, key)
        for key in (
            "body",
            "body_digest",
            "normalization_version",
            "bot_name",
            "reply_name",
        )
    ):
        return action, None
    return action, source_results.confirmed_comment(action, target)


def _matches(payload, question, scope, project):
    _, choice = policy.command(payload, scope["bot_name"], scope["reply_name"])
    return choice == policy.Choice(question["question_id"], question["version"], scope["option"]) and (
        tapd_authority.comment_authority(payload, project) == "allowed" and bool(payload.get("creator_evidence"))
    )


def _first_is_fresh(data, started, config):
    first = data["first"]
    created = utc_timestamp(first.get("created_at_utc"))
    saved = utc_timestamp(data.get("first_saved_at") or first.get("observed_at"))
    changed = first.get("modified")
    modified = tapd_authority.remote_timestamp(changed, config["api_timezone"]) if changed else created
    # TAPD may provide whole-second creation timestamps. An opaque newly
    # issued ID plus first/new body proof bounds same-second replies.
    return (
        created is not None
        and started is not None
        and created == modified
        and saved is not None
        and started <= saved
        and created <= saved
        and created >= (started.replace(microsecond=0) if created.microsecond == 0 else started)
    )


def _known_answer_change(latest, question, scope, project):
    authority = tapd_authority.comment_authority(latest, project)
    return authority in {"comment_deleted", "automated_comment", "mention_removed", "creator_changed"} or (
        authority == "allowed" and not _matches(latest, question, scope, project)
    )


def intercept(conn, config, project, observation, publication, binding, current, *, now):
    """Return command event IDs and persist only confirmed option evidence."""
    ticket = observation.facts.identity
    question = read.current(conn, ticket.source_id)
    scope = scope_for_question(question) if question is not None else None
    offered = policy.supported_scope(config, project, observation.facts, binding)
    complete = observation.coverage == "complete" and observation.facts.body is not None
    facts_known = complete and tapd_authority.label_authority(observation.facts, project).reason not in {
        "ticket_state_unknown",
        "label_facts_unknown",
    }
    if question is not None and scope != offered and facts_known:
        previous_answer = read.answer(conn, question["question_id"])
        if previous_answer is not None:
            write.revoke(conn, previous_answer["event_id"], now)
        write.supersede(conn, question, now)
        question, scope = None, None

    action, confirmed = (
        _publication(conn, question, ticket, scope)
        if question is not None and scope is not None and scope == offered
        else (None, None)
    )
    started = utc_timestamp(action["intent_started_at"]) if action is not None else None
    intercepted = {}
    # Stable event order chooses at most one of multiple equivalent replies.
    for event_id, data in sorted(current.items()):
        first, latest = data["first"], data["latest"]
        original, _ = policy.command(first, config["bot_name"], config["reply_name"])
        reserved, _ = policy.command(latest, config["bot_name"], config["reply_name"])
        if not (original or reserved or str(data.get("disposition", "")).startswith("confirmation_")):
            continue
        accepted = read.answered_event(conn, event_id)
        disposition = "confirmation_refused"
        if accepted is not None:
            original_question = read.question(conn, ticket.source_id, accepted["question_id"])
            original_scope = scope_for_question(original_question) if original_question is not None else None
            applied = read.applied_question(conn, accepted["question_id"])
            invalid = (
                _known_answer_change(latest, original_question, original_scope, project)
                if applied and original_scope is not None
                else original_scope is None or not _matches(latest, original_question, original_scope, project)
            )
            if (complete or latest.get("deleted")) and invalid:
                write.revoke(conn, event_id, now)
                if question is not None and question["question_id"] == accepted["question_id"]:
                    write.supersede(conn, question, now)
                    question, scope, action, confirmed = None, None, None, None
                disposition = "confirmation_revoked"
            else:
                disposition = "confirmation_revoked" if accepted["revoked_at"] else "confirmation_accepted"
        elif (
            complete
            and original
            and scope is not None
            and started is not None
            and _first_is_fresh(data, started, config)
            and _matches(first, question, scope, project)
            and _matches(latest, question, scope, project)
        ):
            if confirmed is None or publication.unknown_intents:
                disposition = "confirmation_pending"
            elif not read.answer(conn, question["question_id"]):
                if write.accept(
                    conn,
                    question_id=question["question_id"],
                    event_id=event_id,
                    publication_id=action["publication_id"],
                    comment_id=confirmed[1],
                    proof=_provenance(first),
                    now=now,
                ):
                    disposition = "confirmation_accepted"
        intercepted[event_id] = disposition
    return intercepted


def _provenance(first):
    return {
        key: first.get(key)
        for key in (
            "creator",
            "creator_evidence",
            "own_body_evidence",
            "body_sha256",
            "created_at_utc",
            "entry_type",
            "external_id",
        )
    }


def validated_receipt(conn, ticket, question, data, project):
    """Revalidate original and latest exact choice and its saved publication evidence."""
    answer = read.answer(conn, question["question_id"])
    if answer is None or answer["revoked_at"]:
        return None
    return _answer_receipt(conn, ticket, question, answer, data, project)


def _answer_receipt(conn, ticket, question, answer, data, project):
    scope = scope_for_question(question)
    if scope is None or data is None:
        return None
    first, latest = data["first"], data["latest"]
    action, confirmed = _publication(conn, question, ticket, scope)
    if (
        not confirmed
        or action["publication_id"] != answer["publication_id"]
        or confirmed[1] != answer["comment_id"]
        or not _matches(first, question, scope, project)
        or not _matches(latest, question, scope, project)
        or json_object(answer["proof_json"]) != _provenance(first)
    ):
        return None
    return ConfirmationReceipt(
        question["question_id"],
        question["version"],
        question["scope_digest"],
        scope["option"],
        answer["event_id"],
        answer["publication_id"],
        answer["comment_id"],
        answer["consumer_id"],
    )


def applied_answer_intact(conn, ticket, binding, project, current):
    """Prove the old answer still exists after its grant was retired by label loss."""
    transition = read.resume_transition(conn, ticket.source_id, binding.get("resume_transition_id"))
    if transition is None or transition["binding_request_id"] != binding.get("request_id"):
        return False
    question = read.question(conn, ticket.source_id, transition["question_id"])
    answer = read.answer(conn, transition["question_id"])
    if question is None or answer is None:
        return False
    receipt = _answer_receipt(conn, ticket, question, answer, current.get(answer["event_id"]), project)
    return receipt is not None and asdict(receipt) == json_object(transition["receipt_json"])


def resume_identity(source_id, question_id, version, scope_digest):
    return "source-resume:" + tapd_authority.digest((source_id, question_id, version, scope_digest))


def resumed_binding(paused, transition_id):
    return {
        **{key: value for key, value in paused.items() if key != "reason"},
        "state": "active",
        "resume_transition_id": transition_id,
    }


def repository_identity(row):
    return {
        key: row[key]
        for key in ("repo_id", "full_name", "repo_root", "worktree_root", "default_base_branch", "github_account")
    }


def same_rule_request(row, scope, repository, *, initial=False):
    saved = json_object(row["authorization_json"])
    return (
        saved is not None
        and saved.get("rule") == scope["old_rule"]
        and saved.get("capability") == SourceCapability.DEVELOPMENT.value
        and saved.get("repos") == [repository["full_name"]]
        and row["repo_id"] == repository["repo_id"]
        and (
            row["auth_type"] == SourceAuthorizationType.LABEL_QUEUE.value
            if initial
            else row["auth_type"]
            in (SourceAuthorizationType.LABEL_QUEUE.value, SourceAuthorizationType.TAPD_MENTION.value)
        )
    )


def original_binding_owned(conn, ticket, scope, repository):
    owner = source_members.owned_request(conn, ticket.source_id, scope["binding_request_id"])
    return (
        owner is not None
        and same_rule_request(owner.request, scope, repository, initial=True)
        and [row["event_id"] for row in owner.events]
        == [json_object(owner.request["authorization_json"]).get("event_id")]
    )


def resume_required(conn, source_id, binding):
    return bool(
        binding
        and binding.get("state") == "active"
        and (
            "resume_transition_id" in binding
            or read.latest_resume_transition(conn, source_id, binding.get("request_id"))
        )
    )


def applied_resume_status(conn, config, project, facts, binding, current, *, complete):
    """Reprove an active transition without using the current-question pointer."""
    ticket = facts.identity
    transition_id = binding.get("resume_transition_id")
    row = read.resume_transition(conn, ticket.source_id, transition_id)
    if row is None or read.latest_resume_transition(conn, ticket.source_id, binding.get("request_id")) != (
        transition_id,
    ):
        return "unavailable", None
    paused = json_object(row["paused_binding_json"])
    repository = json_object(row["repository_json"])
    expected = json_object(row["receipt_json"])
    question = read.question(conn, ticket.source_id, row["question_id"])
    scope = scope_for_question(question) if question is not None else None
    answer = read.answer(conn, row["question_id"])
    if (
        paused is None
        or paused.get("state") != "paused"
        or binding != resumed_binding(paused, transition_id)
        or row["binding_request_id"] != paused.get("request_id")
        or not repository
        or not expected
        or not scope
        or question["superseded_at"] != row["applied_at"]
        or answer is None
        or answer["claimed_at"] != row["applied_at"]
        or expected.get("consumer_id") != transition_id
        or expected.get("event_id") != answer["event_id"]
        or answer["consumer_id"] != transition_id
        or transition_id
        != resume_identity(ticket.source_id, question["question_id"], question["version"], question["scope_digest"])
        or scope["option"] != "resume"
        or scope["old_rule"] != scope["current_rule"]
        or not original_binding_owned(conn, ticket, scope, repository)
    ):
        return "unavailable", None

    event_id = answer["event_id"]
    label = tapd_authority.label_authority(facts, project)
    if answer["revoked_at"] or label.reason == "terminal" or config.get("enabled") is not True:
        return "revoked", event_id
    stored_repo = source_tasks.repository(conn, repository["repo_id"])
    if stored_repo is None or repository_identity(stored_repo) != repository:
        return "revoked", event_id
    if label.reason in {"ticket_state_unknown", "label_facts_unknown"}:
        return "unavailable", event_id
    if policy.supported_scope(config, project, facts, paused) != scope:
        return "revoked", event_id
    if not complete:
        return "unavailable", event_id
    receipt = validated_receipt(conn, ticket, question, current.get(event_id), project)
    return ("current" if receipt is not None and asdict(receipt) == expected else "unavailable"), event_id
