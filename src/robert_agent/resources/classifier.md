# Classifier

Classification turns authorized GitHub events into one of the fixed output types:

- `multi_agent_vote_report` (request intent and route: `vote`)
- `comment_analysis`
- `new_pr`
- `update_existing_pr`
- `review_comment`
- `classification_result`
- `waiting_for_user`

High-confidence bug fixes and small changes can route directly to implementation. Requirement analysis and broad design discussion should route to `comment_analysis` unless a trusted actor clearly approves implementation. Low-confidence inputs must route to a lightweight classification worker or to `waiting_for_user`.

An existing Robert PR does not establish implementation intent. An unclear
follow-up goes through classification; explicit fixes, small changes, tests and
PR follow-up fixes can select `update-existing-pr` directly. A `new-pr`
recommendation for that existing PR is constrained to `update-existing-pr`.

A `classification_result` must return a structured `recommended_route` such as
`new-pr`. If the route or requested outcome remains unclear, recommend
`waiting-for-user` and explain the missing decision in `handoff`. Robert
normalizes a missing or blank route to `waiting-for-user` before persisting the
result. Pending trusted input takes precedence over this automatic fallback.
Otherwise GitHub work gets a follow-up task to ask the user through the normal
audited question flow; local Web work records a clarification question and
enters the waiting state directly. An empty route cannot complete the request.
Free-form handoff text is not a routing signal.

Routes are data, not dispatcher code. Add or reorder route entries in `routes.yml`; keep the agent orchestration independent from route-specific keywords.

The strong vote heuristic requires a trusted request addressed to the configured
bot account with an explicit voting action (投票, 表决, vote), not a reference to a
voting implementation inside a fix/explanation request. Generic comparisons
retain their existing analysis/fix intent; the classifier may recommend `vote`
when the original request actually asks for independent voting.
Ignore quoted/background/negated voting text and requests to implement a voting
feature. A classification result may recommend `vote`; follow-up creation still
checks the original request, trusted actor, enabled route, workers and scope.
Do not initiate voting automatically from review requests or assignments.
