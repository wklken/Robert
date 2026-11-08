## Delivery
Record the structured result before claiming completion. Replace example values with observed results. Explicitly submit only handled event identities from this input set. A handled subset is valid. Do not consume later arrivals.

${result_command} <<'JSON'
${result_example}
JSON

## Ending
Submission acknowledgement:
- If the helper returns failed_validation, correct the indicated field and submit again in this attempt. A rejected submission does not complete the task.
- If it returns validation_unavailable, retry when validation state is available. Claim submission only after a recorded or deferred acknowledgement; deferred means durably saved for ingestion.
- After acknowledgement, stop changing the submitted work and exit. Robert performs its final audit after the worker process stops.

If a missing input or scope decision blocks execution, record --output-type waiting_for_user with an "operator_question" object such as {"kind": "scope_decision", "summary": "<exact missing decision>"}. For GitHub tasks, also plan an allowed comment explaining the question; do not plan a push. recommended_route does not change an execution result's lifecycle.
