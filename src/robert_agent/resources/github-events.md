# GitHub Events

GitHub notifications are polling hints, not canonical task triggers. A
notification thread can point at an issue, pull request, or commit and its
reason can change as the same thread receives later activity. Before creating
or resuming work, the agent must inspect the source issue or PR timeline,
comments, reviews, and review comments, then normalize the hint to the
underlying GitHub event.

For multi-repo configs, account notifications must first be attributed by the
notification payload's `repository.full_name`. The agent must ignore
notifications for unconfigured repositories and must not probe an arbitrary
configured repository using only the issue or PR number from the notification.
Preloaded notification hints follow the same rule: if a hint's
`repo_full_name` does not match the current repo config, ignore it without
calling the source issue or PR API.

## Canonical Events

| Canonical event | GitHub source | Fingerprint | Creates a task? | Notes |
| --- | --- | --- | --- | --- |
| `assigned` | Issue or PR timeline `assigned` event | `assigned:<timeline-event-id>` | Yes, only when the assigner is a trusted actor and the assignee is `github_account`. | Assignment discovery may start from assignee search or from a notification hint; both paths must reuse the same timeline event fingerprint and keep timeline-first resolution ahead of later discussion activity. |
| `comment` | Issue comment, PR conversation comment, or issue/PR body mention lookup | `comment:<comment-id>` or explicit fixture fingerprint | Yes, only when a trusted actor mentions `github_account`; otherwise context only for known workstreams. | Pull requests are issues in the REST API, so PR conversation comments come from issue comment endpoints. |
| `review` | Pull request review body | `review:<review-id>` | No for new work by itself; accepted as follow-up context in known workstreams. | A review can carry actionable text for an existing DD PR workstream. |
| `review_comment` | Pull request diff review comment | `review_comment:<comment-id>` | No for new work by itself; accepted as follow-up context in known workstreams. | Diff comments are distinct from issue comments and must stay distinguishable in prompts and evidence. |
| `review_request` | PR timeline `review_requested` event | `review_request:<timeline-event-id>` | Yes, only when the requester is trusted and the requested reviewer or team matches `github_account`. | Notification reason `review_requested` must be resolved through the PR timeline before authorization. Route to `review-pr` so the worker reviews PR source in a read-only worktree and records a comment-only result. |
| `notification` | `/notifications` thread | `notification:<thread-id>` only while unresolved | No. | If lookup cannot complete, record pending authorization; if lookup completes without an actionable canonical event, ignore it. |

## Trigger Rules

Task triggers:

- trusted actor mentions `github_account`
- trusted actor assigns an issue or PR to `github_account`
- trusted actor requests review from `github_account`
- trusted actor replies to a waiting task with a clear command or natural-language decision

Follow-up context in a known workstream is accepted from trusted actors and from
authors with `author_association` equal to `OWNER`, `MEMBER`, or
`COLLABORATOR`. When `author_association` is absent or `UNKNOWN`, the agent may
query collaborator permission and accept `admin`, `maintain`, `write`, or
`triage`.

Issue follow-up belongs to the issue mainline. PR follow-up belongs to the PR mainline, even when the PR was created from an issue task. The PR keeps `origin_workstream_id` metadata so the agent can relate it back to the issue without serializing PR repair behind the issue workstream.

DD-authored GitHub content never creates a task. It remains available as
context and audit evidence.

Closed issue or PR notification sources do not create follow-up work. Active
workstreams are reconciled against the remote source state before new discovery
is routed: closed issues cancel the active task and close the workstream as
completed or canceled based on `state_reason`; merged PRs cancel any in-flight
PR update task and mark the PR workstream completed; closed unmerged PRs cancel
the PR workstream.

## Realistic Collaboration Cases

1. Issue discussion starts work: a trusted actor mentions `github_account` in an
   issue comment, the agent creates an issue workstream task, later issue
   comments on that active workstream are stored as context or pending events.
2. Issue work produces a PR: a successful worker result publishes an `open_pr`
   action with `robert-workstream` metadata. The issue workstream can complete, and
   the derived PR workstream is materialized with `origin_workstream_id`
   pointing back to the issue.
3. PR discussion fixes the branch: review bodies, issue-style PR comments, and
   diff review comments on the DD-authored PR are routed on the PR workstream.
   They must update the existing PR branch, not reopen or serialize through the
   origin issue workstream.
4. Review request asks for DD attention: a notification with reason
   `review_requested` is only a hint. The canonical trigger is the PR timeline
   `review_requested` event whose requester is trusted and whose requested
   reviewer or team matches `github_account`. This creates a `review-pr` source
   review worktree for the PR head and only permits a planned comment action.
5. Closed or merged sources stop creating new work: closed issue/PR notification
   hints are ignored during discovery. Active-task reconciliation must query the
   source issue or PR directly, cancel in-flight work when the source is closed,
   and complete a DD PR workstream when the PR was merged.
