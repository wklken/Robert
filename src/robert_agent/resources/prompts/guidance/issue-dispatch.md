## issue-dispatch
Issue dispatch:
The authenticated original request determines the work; the user need not choose a delivery format.
For classification_result, return recommended_route: comment-analysis for research/analysis, new-pr for requested implementation, or waiting-for-user for material ambiguity. Never omit recommended_route. Do not infer implementation permission merely because analysis discovers a defect.
For new_pr, plan the linked PR and a result comment on the original Issue. Robert adds publication markers; do not supply control markers yourself. Analysis completes with a substantive result comment on that Issue. Do not merge the PR or close the Issue.
