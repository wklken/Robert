# Trust Model

Only configured trusted actors can start new tasks. The first repo configuration enables `wklken` as the trusted actor for `example/backend`.

Once a workstream is known, including an inactive completed workstream, follow-up input can come from:

- trusted actors
- `OWNER`
- `MEMBER`
- `COLLABORATOR`

If `author_association` is absent or unknown, the agent may query repository collaborator permission. If permission cannot be confirmed, record `pending_actor_permission` and do not let that event drive execution.

Accepted context on a known inactive workstream is recorded for evidence. It can reopen that workstream only when a trusted actor replies, except that a DD-derived PR follow-up with origin metadata can create an update task for that PR. Active workstreams continue to receive follow-up as pending context for the active task.

Waiting tasks resume only when a trusted actor replies. Collaborator replies can add context but cannot approve scope changes, implementation, cancellation, or route selection.
