"""Capture terminal process metering independently from result acceptance."""

from robert_agent.adapters.processes import model_usage
from robert_agent.foundation.values import utc_now
from robert_agent.persistence.stores import model_usage as store


def record_dispatch_usage(conn, root, kind, call_id, task_id, dispatch, *, observed=None):
    if root is None or not isinstance(dispatch, dict):
        return
    observed = model_usage.read_dispatch(root, dispatch) if observed is None else observed
    if observed.get("usage_available"):
        store.record(conn, kind, call_id, task_id, utc_now(), observed)
