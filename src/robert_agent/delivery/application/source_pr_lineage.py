"""Apply or recover only controller-proved native PR links."""

from robert_agent.adapters.github.discovery import source_key
from robert_agent.foundation import values
from robert_agent.foundation.contracts.delivery import GitHubActionType
from robert_agent.persistence import errors, transactions
from robert_agent.persistence.queries import (
    source_github_publication as github,
    source_pr_lineage as read,
    source_results,
    source_sessions,
)
from robert_agent.persistence.stores import source_pr_lineage as write, source_sessions as fence


def _link(conn, claim, root_id, action_id, now):
    confirmed = github.confirmed_pr(conn, root_id, action_id)
    if confirmed is None:
        return False
    receipt, prepared, pr = confirmed
    identity = prepared["identity"]
    source_id, repo_id = identity["source_id"], identity["repo_id"]
    if source_id != claim.source_id:
        return False
    origin = read.origin_workstream(conn, source_id, repo_id)
    stream = read.task_workstream(conn, identity["task_id"])
    if origin is None or stream is None:
        return False
    if identity["action_type"] == GitHubActionType.OPEN_PR:
        if stream != origin:
            return False
        key = source_key(identity["repository"], "pull_request", pr["number"])
        if read.github_pr_source(conn, identity["repository"], pr["number"]) is None:
            write.insert_pr_source(
                conn,
                source_id=values.github_source_id(key),
                source_key=key,
                repo_id=repo_id,
                repository=identity["repository"],
                receipt=receipt,
                pr=pr,
            )
        source = read.github_pr_source(conn, identity["repository"], pr["number"])
        if source is None or source["repo_id"] != repo_id or not read.same_pr_url(source["html_url"], receipt.url):
            return False
        streams = read.workstream_for_pr(conn, source["source_id"])
        if len(streams) > 1:
            return False
        stream_id = streams[0]["workstream_id"] if streams else key
        creation = read.creation_evidence(receipt, prepared)
        if streams:
            existing = read.confirmed_pr_lineage(
                conn, source_id=source_id, repo_id=repo_id, workstream_id=stream_id, action_id=action_id
            )
            if existing is not None:
                return True
            if streams[0]["active_task_id"] is not None or source_sessions.workstream_adoption_blocked(conn, stream_id):
                return False
            if not write.attach_creation(
                conn,
                stream_id=stream_id,
                repo_id=repo_id,
                source_id=source["source_id"],
                origin_id=origin[0],
                creation=creation,
            ):
                return False
            write.relate_pr_source(conn, stream_id=stream_id, source_id=source["source_id"], now=now)
        else:
            write.insert_pr_stream(
                conn,
                stream_id=stream_id,
                repo_id=repo_id,
                source_id=source["source_id"],
                origin_id=origin[0],
                creation=creation,
                now=now,
            )
        return (
            read.confirmed_pr_lineage(
                conn, source_id=source_id, repo_id=repo_id, workstream_id=stream_id, action_id=action_id
            )
            is not None
        )
    links = read.linked_pr_lineages(conn, source_id=source_id, repo_id=repo_id)
    matches = tuple(
        link
        for link in links
        if (
            link.workstream_id,
            link.number,
            link.url,
            link.head_repository,
            link.head_branch,
            link.author,
            link.base_branch,
        )
        == (
            stream[0],
            pr["number"],
            receipt.url,
            pr["head_repository"],
            pr["head_branch"],
            pr["author"],
            pr["base_branch"],
        )
    )
    if len(matches) != 1:
        return False
    write.save_pr_observation(conn, source_id=matches[0].pr_source_id, pr=pr, observed_at=receipt.observed_at)
    return True


def ensure_root_pr_lineage(conn, claim, *, source_result_id, now):
    """Materialize or validate the exact PR-bearing receipt under the source writer."""
    if not conn.in_transaction or not fence.lock_claim(conn, claim, now=now):
        return False
    root = source_results.for_source(conn, claim.source_id, source_result_id)
    if root is None:
        return False
    actions = source_results.required_actions(conn, source_result_id)
    sets = source_results.required_action_sets(conn, root, actions)
    if sets is None:
        return False
    base, _feedback = sets
    github_actions = [row for row in base if row["provider"] == "github"]
    if not github_actions:
        return True
    if base[-1]["provider"] != "tapd":
        return False
    source = source_results.tapd_source(conn, claim.source_id)
    if (
        source is None
        or source_results.confirmed_required_effects(
            conn, source_result_id, source_results.source_target(source), before_position=base[-1]["position"]
        )
        is None
    ):
        return False
    action = github_actions[-1]
    checkpoint = "ensure_native_pr_lineage"
    transactions.savepoint(conn, checkpoint)
    try:
        linked = _link(conn, claim, source_result_id, action["action_id"], now)
    except (ValueError, errors.IntegrityError):
        transactions.finish_isolation(conn, checkpoint, rollback=True)
        return False
    except BaseException:
        transactions.finish_isolation(conn, checkpoint, rollback=True)
        raise
    transactions.finish_isolation(conn, checkpoint, rollback=not linked)
    return linked
