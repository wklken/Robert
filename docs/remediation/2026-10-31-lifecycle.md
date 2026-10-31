# Lifecycle recovery after the SQLite cutover

This change closes the following event-to-terminal-state gaps without replacing
historical results, resetting execution recovery budgets or adding a schema migration.

| Finding | Corrected behavior | Regression coverage |
| --- | --- | --- |
| L1 | Repository/task failures retain their scope through the loop and daemon; local task preparation failure stops that task with an operator notice. Controller crashes and deadline failures keep the lane stop policy. | `test_lifecycle_followup.py` |
| L2 | Quota deferral schedules a future scan/publish without consuming the permanent control-failure budget. | `test_lifecycle_followup.py` |
| L3 | Queue admission accounts for both repository and global capacity; rejected candidates do not consume later repositories' dispatch budget. | `test_lifecycle_followup.py` |
| L4 | Known-source notifications retain every new discussion identity even when a historical assignment exists and search quota is unavailable. Current trust still decides which events execute. | `test_notification_lifecycle.py` |
| L5 | Ordinary saved GitHub requests revalidate current trust before dispatch, including assignment/review-request requester semantics. Explicit local operator requests retain their separate authorization. | `test_lifecycle_followup.py`, existing workflow matrix |
| L6 | Git recovery/audit observations use bounded retry state and due times. Exhausted audit observations preserve the result and expose Retry for that same result. | `test_lifecycle_followup.py` |
| L7 | A removed manually selected worker stops automated attempts until the operator changes the worker and retries. | `test_lifecycle_followup.py` |
| L8 | Paused supervision resolves persisted repository IDs by full name; existing IDs survive configuration upsert. | `test_lifecycle_followup.py` |
| LC-1 | Superseded ordinary PR updates settle their publication intents and stop with a visible instruction to retry against the current branch. Skipped actions are not reported as published. | `test_publication_lifecycle_followup.py` |
| LC-2 | Normal and paused supervision serialize destructive decisions with result submission/replay; a durable pending or in-flight submission retains its execution until ingestion. | `test_lifecycle_followup.py`, `test_lifecycle_remediation.py` |
| LC-3 | Remote closure retires actions known not to have started. Ambiguous effects retain read-only reconciliation. Reopened sources accept fresh trusted requests; explicit operator cancellation remains stopped. | `test_publication_lifecycle_followup.py`, `test_board_lifecycle.py` |
| LC-4 | Native failed launches release a model reservation only when the saved outcome proves no process was created; unknown launches still require inspection. | `test_native_launch_slot_recovery.py` |
| LC-5 | Remediation questions publish before the Episode ends. GitHub and Board replies create fresh human work against the question's actual workstream. | `test_publication_lifecycle_followup.py` |

Submission receipt, process exit, accepted audit, publication receipt and business
completion remain separate facts. The result spool lock covers the terminal
transaction; network and Git observations stay outside the SQLite writer. An
unknown process group or ambiguous publication never becomes successful merely
because a retry budget elapsed.

Recovery is scoped. Repair the reported cause and use the Board action shown for
the stopped item. Audit Retry retains its Task/Attempt/result. A superseded update
requires fresh work on the current head. Controller-level attention still requires
repairing its cause and restarting the daemon; quota waits do not.

Deployment should preserve the previous environment, configuration, result spool
and a consistent database backup. Do not restore an old database after new
external effects; reconcile forward. The change does not modify Web access
configuration or worker credentials.
