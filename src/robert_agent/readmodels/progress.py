"""Read scoped controller progress without claiming or changing work."""

from robert_agent.persistence.queries.readviews import counts


def snapshot_progress(conn, repo_ids=()):
    return counts.controller_progress(conn, repo_ids)


def diff_progress(before, after):
    before_totals, before_states = before
    after_totals, after_states = after
    changed = {
        key: {"before": before_totals[key], "after": value}
        for key, value in sorted(after_totals.items())
        if before_totals[key] != value
    }
    domains = [name for name, state in sorted(after_states.items()) if before_states[name] != state]
    return {"changed": bool(changed or domains), "changed_fields": changed, "changed_domains": domains}
