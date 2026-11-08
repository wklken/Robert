## Task
Classify the current request and identify the authorized follow-up route.

## Work
Classification scope:
- This run is only for task classification and follow-up route selection; it is not the final implementation or execution worker.
- Do not modify files, create branches, open PRs/comments, or perform broad verification.
- Decide quickly from the task description, title/body, linked context, and any changed-file or module list already provided.
- If a focused lookup is needed, inspect only the named files/modules or their immediate owners. Avoid broad repo exploration, deep dependency tracing, and expensive test suites.
- Record a concise recommended_route and handoff with the key evidence for that route.
- When recommended_route is new-pr, record a concise meaningful lowercase English kebab-case branch_slug based on the requested change.

## Delivery
Classification result:
Set recommended_route to new-pr, update-existing-pr, review-comment, review-pr, comment-analysis, vote or waiting-for-user when follow-up work is needed; leave it empty only when no follow-up is needed.
For new-pr, set branch_slug to meaningful lowercase English kebab-case, at most 60 characters; otherwise leave it empty. Put the routing evidence in handoff.
