# Trust Model

Only configured `trusted_actors` can start, resume, or redirect workers through
GitHub events, including follow-up requests on known workstreams. Repository
roles, collaborator permissions, PR authorship, and previous review participation
do not grant execution authority. Assignment and review requests must be traced
to a trusted requester.

Messages from other actors remain recorded GitHub context and may be associated
with an active task as discussion data. They do not refresh worker instructions,
resume a waiting task, or independently reopen a completed workstream. Saved
pending events are checked against the current trust policy before child creation. Waiting tasks resume only on a trusted actor's explicit reply.

Mentions must address the configured account exactly, outside quotes and code.
A mention or CSRF token is not authentication.

Model-worker environment allowlists cannot include GitHub credentials. Native
review coordinators are trusted controller code and retain credentials for PR
metadata reads and snapshot preparation; their model children use filtered
worker environments. This boundary is not an operating-system sandbox.
