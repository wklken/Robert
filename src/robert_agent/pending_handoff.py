"""Durable unfinished event handoffs after an accepted result is completed."""
from robert_agent import execution_store, result_contract


def ready_rows(conn, repo_ids, now):
    if not repo_ids:
        return []
    placeholders = ','.join('?' for _ in repo_ids)
    return conn.execute(f"""SELECT t.task_id,t.workstream_id FROM tasks t
        JOIN workstreams w ON w.workstream_id=t.workstream_id
        JOIN attempts a ON a.task_id=t.task_id
        JOIN worker_results wr ON wr.attempt_id=a.attempt_id
        WHERE w.repo_id IN ({placeholders}) AND w.lifecycle='active'
          AND w.active_task_id=t.task_id AND t.lifecycle='completed'
          AND NOT EXISTS(SELECT 1 FROM work_items wi WHERE wi.workstream_id=w.workstream_id AND wi.canceled_at IS NOT NULL)
          AND NOT EXISTS(SELECT 1 FROM github_sources gs WHERE gs.source_id=w.primary_source_id AND lower(gs.state)='closed')
          AND a.status='completed' AND wr.output_type NOT IN ('classification_result','waiting_for_user')
          AND json_extract(wr.metadata_json,'$.audit.status')='accepted'
          AND json_extract(wr.metadata_json,'$.result_contract')=?
          AND (json_type(a.metadata_json,'$.dispatch.pid') IS NOT 'integer'
               OR json_extract(a.metadata_json,'$.process_stopped_at') IS NOT NULL)
          AND COALESCE(json_extract(t.metadata_json,'$.pending_handoff.retryable'),1)!=0
          AND (json_extract(t.metadata_json,'$.pending_handoff.next_retry_at') IS NULL
               OR julianday(json_extract(t.metadata_json,'$.pending_handoff.next_retry_at'))<=julianday(?))
          AND EXISTS (SELECT 1 FROM task_events te WHERE te.task_id=t.task_id
                      AND te.relationship IN ('context','pending'))
          AND NOT EXISTS (SELECT 1 FROM tasks child WHERE child.parent_task_id=t.task_id)
          AND NOT EXISTS (SELECT 1 FROM github_actions ga WHERE ga.result_id=wr.result_id
                          AND (ga.audit_status!='accepted' OR ga.publish_status!='published'))
          AND EXISTS (SELECT 1 FROM execution_subjects owner WHERE owner.binding_status='active'
                      AND ((t.task_kind='human_request' AND owner.subject_type='task' AND owner.subject_id=t.task_id)
                           OR (t.task_kind IN ('ci_remediation','merge_conflict_remediation')
                               AND owner.subject_type='remediation_episode'
                               AND owner.subject_id=json_extract(t.metadata_json,'$.remediation_episode_id'))))
          AND {execution_store.runnable_sql()}
        ORDER BY t.created_at,t.task_id""", [*repo_ids, result_contract.CONTRACT_ID, now]).fetchall()
