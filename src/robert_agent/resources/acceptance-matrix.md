# Acceptance Matrix

This matrix is the operator-facing checklist for whether the Robert can
collaborate with ordinary human developers across GitHub issues, DD-created PRs,
and third-party PRs.

Current scope: these rows are fixture-backed control-plane acceptance tests.
They prove event normalization, authorization, routing, workstream/task state,
prompt shape, result audit, publication gating, and operator evidence. This is
not yet a live GitHub/worker acceptance test; live credentials, real
repositories, real checkouts, real worktrees, and real worker execution must be proven separately
before claiming production readiness.

| Scenario ID | Human workflow | Expected agent behavior | Proof |
| --- | --- | --- | --- |
| issue-assignment-analysis | A trusted user assigns an issue or mentions the DD account for analysis. | Create an issue workstream, route to `comment-analysis`, avoid a worktree, and render the accepted event into the worker prompt. | `tests/dd_github_agent/test_workflow_matrix.py::WorkflowMatrixTests.test_issue_assignment_for_analysis_creates_comment_workflow` |
| issue-assignment-new-pr | A trusted user asks the DD account to fix an issue. | Create a `new-pr` task, accept worker results with `push_existing_pr` then `open_pr` only after audit, publish-gated completion, and materialize a DD PR workstream linked back to the issue. | `tests/dd_github_agent/test_workflow_matrix.py::WorkflowMatrixTests.test_issue_assignment_bugfix_result_materializes_dd_pr_workflow` |
| issue-active-followup-context | A repository member comments with extra context while an issue task is active. | Do not start a second active task; attach the event as context on the existing workstream. | `tests/dd_github_agent/test_workflow_matrix.py::WorkflowMatrixTests.test_issue_followup_comment_stays_on_existing_workflow_context` |
| dd-created-pr-review-followup | A reviewer comments on a DD-created PR and asks for follow-up fixes. | Route to `update-existing-pr`, keep the PR on its own mainline, reuse the existing PR branch, include the review event in the prompt, and require a response comment for the review report. | `tests/dd_github_agent/test_workflow_matrix.py::WorkflowMatrixTests.test_dd_pr_followup_routes_to_existing_pr_workflow` |
| multi-repo-account-cycle | One GitHub account receives actionable events from two configured repos with different trusted actors. | One `run_once` attributes notifications by `repository.full_name`, applies each repo's trust policy, prepares work in the matching local checkout, and reports repo-level partial failures without blocking other repos. | `tests/dd_github_agent/test_run_once.py::RunOnceDryRunTests.test_run_once_processes_two_repos_with_repo_specific_trust` |
| pr-reviewer-assignment | A trusted user requests DD review on a PR through GitHub reviewer assignment. | Resolve the notification through the PR timeline, route to `review-pr`, create a PR source review worktree from `pull/<number>/head`, require `fast-review-github-pr`, and allow only a planned comment action. | `tests/dd_github_agent/test_workflow_matrix.py::WorkflowMatrixTests.test_review_request_creates_source_review_worktree` |
| third-party-pr-question | A trusted user asks the DD account a question on someone else's PR. | Route to `review-comment`, allow comments only, and avoid opening or updating a DD PR. | `tests/dd_github_agent/test_workflow_matrix.py::WorkflowMatrixTests.test_third_party_pr_question_routes_to_review_comment_workflow` |
| third-party-pr-fix-request | A trusted user asks the DD account to fix someone else's PR. | Route to `review-comment`, allow comments only, avoid preparing a worktree, and avoid opening or updating a DD PR. | `tests/dd_github_agent/test_workflow_matrix.py::WorkflowMatrixTests.test_third_party_pr_fix_request_stays_comment_only` |
| trusted-waiting-user-resume | The agent asks a clarifying question, members add context, then a trusted actor confirms implementation. | Keep the original task waiting only after the question is published, collect member context without resuming, then create a child implementation task when a trusted actor replies. | `tests/dd_github_agent/test_workflow_matrix.py::WorkflowMatrixTests.test_trusted_reply_resumes_waiting_for_user_workflow` |

## Operator Evidence

The workflow matrix proves behavior; operator evidence proves a stuck run can be
followed without manual SQLite archaeology. These checks live mostly in
`tests/dd_github_agent/test_operational_commands.py` and cover:

- `operator_alerts` and `operator_next_steps` for attention tasks, stale workers,
  publish waits, publish failures, skipped publication, pending actor permission,
  and pending authorization lookup.
- Summary counters for rejected worker results, publish backlog, skipped publish
  actions, pending actor-permission events, and pending authorization events.
- Recent route decisions, worker results, used skills, event-flow counts, run
  steps, notifications, and artifact paths.
- Non-trusted owner/member/collaborator context on an inactive known non-DD-PR
  workstream is recorded as evidence. Trusted actor context can reopen the
  workstream, and DD-derived PR follow-up with origin metadata can create a
  constrained update task for that PR.

## Remaining Live Acceptance

Before calling the full GitHub collaboration goal complete, run and document a
live acceptance pass that covers at least:

1. Real GitHub discovery with `gh` authentication.
2. Real worktree preparation against a local checkout.
3. Real worker launch through each configured named worker's adapter and
   command, including at least one route-selected non-default worker.
4. Real publication or explicitly dry-run-safe publication of comments/PRs.
5. Retry/dedupe behavior against existing `robert-comment` and `robert-workstream`
   markers.

Use `scripts/acceptance.py --config <config>` as the first live-readiness
preflight. It is read-only: it checks the config, repo checkout, worktree root,
current control-plane dispatch state, `gh` availability/authentication, and the
configured worker command without publishing GitHub content or launching a
worker. A passing preflight only proves the local prerequisites are ready.

Use `scripts/acceptance_suite.py --config <config>` as the operator-facing
summary. It runs the safe acceptance checks, aggregates their evidence, and
keeps the overall status `incomplete` until an explicitly approved production
GitHub write canary has been run. Add `--include-live-worker` when the configured
worker command should be exercised as part of the suite.
Add `--production-canary-target-url <github-issue-or-pr-url>` to have the suite
prepare a no-write canary plan and exact follow-up command; this still does not
write GitHub content.
After an approved canary has published a comment, add
`--production-canary-evidence-url <github-comment-url>` plus the same
`--production-canary-marker-id` to verify the live comment by read-only `gh api`
lookup and let the suite report `completed`.

Use `scripts/controlled_e2e_acceptance.py --config <config>` to prove a
controlled issue-to-PR workflow can complete through the real `run_once`
orchestrator. It uses an isolated local checkout, fixture discovery, real git
worktree creation, a mock worker that records a structured `open_pr` result,
normal audit, fake GitHub PR publication, and final task/workstream
finalization. It does not write production GitHub content, but it is the closest
safe end-to-end acceptance pass for the full local control plane.

Use `scripts/live_discovery_acceptance.py --config <config>` to prove real
GitHub discovery can run through `gh` without mutating the control-plane
database. It runs the live assigned issue search, mention search, notification
lookup, and event normalization path, then reports counts and sample normalized
events without authorizing, routing, or dispatching work.

Use `scripts/live_worker_acceptance.py --config <config>` after preflight to
prove the configured worker command can run against an isolated control-plane
database. The script creates a temporary config/data directory, dispatches a
safe analysis fixture through the real worker command, waits for a structured
worker result, then runs audit and publication in dry-run mode. This covers live
worker launch plus dry-run-safe publication evidence without modifying the
operator's production `dd.sqlite3` or writing GitHub content. It still does not
replace a real GitHub discovery/worktree/publication pass for rows 1, 2, and 5.

Use `scripts/live_worktree_acceptance.py --config <config>` to prove the
worktree path with real git commands. The script creates an isolated bare
upstream and local checkout, routes a safe `new-pr` fixture, runs
`git fetch upstream <base>` and `git worktree add`, verifies the resulting
branch/worktree, and terminates its mock worker. It does not touch the
operator's production checkout or write GitHub content, so it proves row 2 as a
controlled local-git acceptance pass.

Use `scripts/publish_dedupe_acceptance.py` to prove retry-safe publication
dedupe without writing GitHub content. It creates an isolated control-plane
database with accepted comment and open-PR actions, simulates GitHub lookup
responses that already contain matching `robert-comment` and `robert-workstream`
markers, and verifies the publisher marks both actions published/deduplicated
without issuing create-comment or create-PR commands.

Use `scripts/production_write_canary.py --target-url <github-issue-or-pr-url>`
to prepare an isolated canary database and dry-run publication plan. It exits
without calling `gh` unless `--confirm-github-write` is present. With explicit
confirmation, it publishes exactly one `robert-comment` marker-protected comment, or
deduplicates an existing comment with the same marker, through the normal
`publish.py` audited action path. Run this only against an explicitly approved
target issue or PR.
