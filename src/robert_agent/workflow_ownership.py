"""Process-local authority for one durable controller invocation.

Context variables isolate concurrent workflows. They never transfer ownership;
all mutators still verify the persisted binding before acting.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from robert_agent import workflow_store

creation_mode = ContextVar("robert_creation_mode", default="legacy")
subject = ContextVar("robert_workflow_subject", default=None)


@contextmanager
def executing(subject_type, subject_id):
    token = subject.set((subject_type, subject_id))
    mode = creation_mode.set("active")
    try:
        yield
    finally:
        creation_mode.reset(mode)
        subject.reset(token)


def creation_scope(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        config = kwargs.get("config_result") or {}
        token = creation_mode.set(
            (config.get("workflow_engine") or {}).get("mode", "legacy")
        )
        try:
            return function(*args, **kwargs)
        finally:
            creation_mode.reset(token)

    return wrapped


def allows(conn, subject_type, subject_id, *, terminal=False):
    if subject.get() != (subject_type, subject_id):
        return False
    binding = workflow_store.get_binding(conn, subject_type, subject_id)
    return bool(
        binding
        and binding["owner_engine"] == "dbos"
        and binding["binding_status"]
        in ({"active", "terminal"} if terminal else {"active"})
    )


def legacy_runnable_sql(alias="t"):
    return f"""NOT EXISTS (SELECT 1 FROM workflow_bindings owner WHERE
      (owner.owner_engine='dbos' OR owner.binding_status='attention') AND
      ((owner.subject_type='task' AND owner.subject_id={alias}.task_id) OR
       (owner.subject_type='remediation_episode' AND owner.subject_id=json_extract({alias}.metadata_json,'$.remediation_episode_id'))))"""
