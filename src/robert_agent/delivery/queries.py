"""delivery queries responsibilities."""

from robert_agent.execution import retry as retry_policy

from robert_agent.delivery import values as publication_values


def ready_actions(conn, limit, repo_id=None, task_ids=None, action_ids=None):
    repo_filter = ""
    params = [publication_values.now()]
    if repo_id:
        repo_filter = """
          AND EXISTS (
            SELECT 1
            FROM tasks t
            JOIN workstreams w ON w.workstream_id = t.workstream_id
            WHERE t.task_id = github_actions.task_id
              AND w.repo_id = ?
          )
        """
        params.append(repo_id)
    if task_ids is not None:
        repo_filter += " AND github_actions.task_id IN (" + ",".join("?" for _ in task_ids) + ")"
        params.extend(task_ids)
    if action_ids is not None:
        if not action_ids:
            return []
        repo_filter += " AND github_actions.action_id IN (" + ",".join("?" for _ in action_ids) + ")"
        params.extend(action_ids)
    repo_filter += """ AND NOT EXISTS (SELECT 1 FROM github_actions earlier
        JOIN publication_intents intent ON intent.action_id=earlier.action_id
        WHERE earlier.result_id=github_actions.result_id AND earlier.publish_status='not_published'
          AND json_extract(intent.remote_evidence_json,'$.supervision_retry_required')=1
          AND (earlier.created_at<github_actions.created_at OR (earlier.created_at=github_actions.created_at
               AND earlier.action_id<github_actions.action_id)))"""
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='supervision_recovery_actions' AND type='table'"
    ).fetchone():
        repo_filter += """ AND NOT EXISTS (SELECT 1 FROM supervision_recovery_actions recovery
            WHERE recovery.scope_type='task' AND recovery.scope_id=github_actions.task_id
              AND recovery.action_kind IN ('publication_retry','publication_reconcile')
              AND recovery.status IN ('applying','observing','unknown')
              AND (recovery.status IN ('applying','unknown') OR recovery.target_id=github_actions.action_id)
              AND NOT (recovery.target_id=github_actions.action_id AND EXISTS(
                  SELECT 1 FROM publication_intents pending WHERE pending.action_id=github_actions.action_id
                    AND pending.status='executing')))"""
    repo_filter += """ AND NOT EXISTS (SELECT 1 FROM execution_subjects control JOIN tasks own ON own.task_id=github_actions.task_id
        WHERE control.binding_status='attention' AND ((control.subject_type='task' AND control.subject_id=own.task_id)
        OR (control.subject_type='remediation_episode' AND control.subject_id=json_extract(own.metadata_json,'$.remediation_episode_id'))))"""
    repo_filter += """ AND EXISTS (SELECT 1 FROM tasks t WHERE t.task_id=github_actions.task_id
        AND json_extract(t.metadata_json,'$.execution_attention') IS NULL)"""
    return [
        dict(row)
        for row in conn.execute(
            f"""
            SELECT action_id, result_id, task_id, action_type, target_url, external_id,
                   audit_status, publish_status, created_at, metadata_json
            FROM github_actions
            WHERE {retry_policy.publication_ready_sql()}
              {repo_filter}
            ORDER BY created_at, action_id
            LIMIT ?
            """,
            params + [limit],
        )
    ]


def retry_budget_exhausted(prior, now):
    first = prior.get("first_failed_at") or now
    elapsed = (retry_policy.timestamp(now) - retry_policy.timestamp(first)).total_seconds()
    return (
        int(prior.get("attempt_count", 0)) >= retry_policy.MAX_ATTEMPTS or elapsed >= retry_policy.MAX_ELAPSED_SECONDS
    )
