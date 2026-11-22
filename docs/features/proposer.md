# Proposer

Proposer looks for recent, open GitHub Issues and TAPD stories or bugs in explicitly configured scopes. It assesses tickets with code excerpts pinned to the repository's default-branch commit. Proposals include a bounded scope, verification steps and cited code. Browse them at /proposals.

## Configure

Enable the built-in scanner in the v2 configuration. Operators can start with external comments disabled while checking proposal quality.

~~~yaml
proposer:
  enabled: true
  publish_comments: false
  github_repositories: ["org/repo"]
  github_assignees: ["wklken"]
  tapd_projects: ["tapd.example.com/20001234"]
  tapd_assignees: ["kunliangwu"]
  gateway_metadata: /path/to/local-messages-gateway.json
  model: your-local-gateway-model
~~~

Repository names and TAPD projects must already be configured. TAPD uses the existing CLI login and a unique matching repository label. The Git checkout must have the upstream remote with the configured default branch. The local Messages gateway metadata file must be owned by the service user and writable only by its owner. The gateway can use its existing authentication. Robert sends redacted ticket fields, recent discussion and bounded code excerpts with model tools disabled. Enable Proposer only for sources permitted to be sent to that gateway.

The resident daemon scans once a day after 09:00 in Asia/Shanghai. It considers tickets created in the last seven days. Unknown assignees are excluded. Some TAPD CLI versions omit empty assignee fields, so those unassigned tickets are also excluded until the adapter can distinguish an empty field from unavailable metadata. Configuration can allow selected known assignees.

## Review and execute

With Web writes enabled, open a proposal and select **处理** (approve), **暂缓** (hold), or **放弃** (discard). Approval durably enqueues a local admission request for the current version. Robert rechecks the source and cited evidence before admitting it to the existing work queue. If the source changed, or a conflicting workstream is active, the proposal shows the blocking reason. A later scan can reassess a blocked proposal after its discovery window closes and present a new version for approval. Use the board for normal task controls and PR review.

When operator messaging covers the repository, Robert sends one notice for a new proposal. With publish_comments enabled, Robert also posts a short receipt on the source ticket with the proposal number, and reports material execution events. Source comments use existing publisher credentials and are independent of approval. A delivery timeout triggers read-back before another write. The proposal page keeps separate execution and comment delivery states.
