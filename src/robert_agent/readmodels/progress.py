"""Read aggregate controller progress without claiming or changing work."""

from robert_agent.persistence.queries.readviews import counts


def snapshot_progress(conn):
    return counts.controller_progress(conn)


def diff_progress(before, after):
    changed = {
        key: {"before": before.get(key), "after": after.get(key)}
        for key in sorted(set(before) | set(after))
        if before.get(key) != after.get(key)
    }
    return {"changed": bool(changed), "changed_fields": changed}
