"""Task-scoped access to native model round lifecycles."""
from robert_agent import review_store, vote_store
from robert_agent.model_execution import terminate_children as stop_children

STORES = (review_store, vote_store)


def owner(conn, task_id):
    for store in STORES:
        entry = store.session(conn, task_id=task_id)
        if entry:
            return store, entry
    return None, None


def session(conn, *, task_id):
    return owner(conn, task_id)[1]


def terminate_children(conn, task_id):
    store, entry = owner(conn, task_id)
    return stop_children(conn, task_id, store=store) if entry else 'already_dead'
