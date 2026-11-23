# Operator messaging

[文档目录](../README.md) · [功能专题](README.md)

Robert can send worker questions, approval requests and failure notifications to
OpenClaw and receive explicit responses from an authorized operator. Messaging
is disabled by default. The existing status/artifact commands remain available.
Optional [Issue dispatch](issue-dispatch.md) adds `/robert-inbox new/submit/status/retry`
with a separate `messaging.dispatch_enabled` switch.

## Design and ownership

The worker submits its normal `waiting_for_user` result. After audit and required
publication, Robert records the question and its notification in one SQLite
transaction. Worker processes do not hold a chat connection or wait indefinitely:
a reply creates a child task using the existing operator lifecycle, preserving
parentage, accepted context and recovery budgets. GitHub-origin questions still
require their public GitHub comment; chat provides an additional response path.
Replies are recorded in Robert's operator timeline, not posted as the user's
GitHub identity.

The same outbox carries supervisor incidents and exhausted-recovery notices.
Existing automatic recovery policy is unchanged. Safe automatic recovery remains
quiet; an existing operator decision event exposes its approval/cancel choices.
An incident notice without an actionable work-item event supports acknowledgement
only. Acknowledging it records who received it and their note; it does not dismiss
the incident, retry execution, release a process or approve a repair.

Canceled PR workstreams are excluded from automatic repair scans and pending
repair-notification delivery. Cancellation is checked again after collecting
remote health evidence and when preparing a message for transport. Historical
records remain available for local inspection.

CI repair notices group the same PR head, failure reason and CI run/attempt.
Advancing the target branch updates the existing notice's latest repair episode
and base commit without another message. Each repair episode still has its own
snapshot and budget. Updates preserve the original notice time, delivery retries
and acknowledgement. A new CI run, rerun, changed cause or failure after recovery
can notify again. Recovery and a new PR head close outdated CI notices;
acknowledgement alone does not close them. Conflict notices retain snapshot identity.

Fallback health checks report queue delays only when a queued task is older
than five minutes and has no occupied attempt, including a launch in progress,
a running or stale Worker, or a recorded process awaiting exit confirmation.
A successful Worker launch updates the queued task to running in the same transaction as the
attempt record. Accepted operations still unpublished after two minutes use
a separate publication-delay reason. Both notices explain the specific wait
and support acknowledgement only.

| Owner | Responsibility |
| --- | --- |
| `work/messaging.py` | Attention event/outbox correlation and response action policy |
| `persistence/queries/notifications.py`, `persistence/queries/work_item_events.py` | Scoped inbox, delivery readiness and attention facts |
| `persistence/stores/notifications.py` | Notification correlation, resolution and delivery/acknowledgement records |
| `delivery/messages.py` | Scoped, redacted `robert.message.v1` envelopes |
| `delivery/application/notifications.py` | Durable delivery state and bounded retries |
| `adapters/notifications/messaging.py` | OpenClaw CLI and existing command transport |
| `work/application/messaging.py` | Sender/session authorization and operator command admission |
| `integrations/openclaw.py` | Translate authenticated plugin command context to the local CLI |

Messaging uses the existing SQLAlchemy mappings through named `persistence` operations.
Business modules do not construct SQL or import a database driver. Queries return
materialized records; writes participate in the caller-owned transaction without
committing it. Inbox and delivery apply repository and unresolved-attention filters
before limiting results, so closed or unauthorized notices cannot starve eligible work.
Chat reads use an existing database in read-only mode; acknowledgement uses an
existing writable database and an immediate transaction. Neither path creates a
missing database. The response transport reuses existing tables; Issue dispatch adds its own durable request table. No new runtime dependency is required. Disabling messaging
stops the built-in transport and rejects new chat responses; the timeline and
local controls continue to work.

## Configure OpenClaw

Install or rewrite the generated plugin after upgrading Robert:

```bash
robert openclaw install --force
robert openclaw status
```

From the intended chat, run `/robert-messaging-context`. Copy the exact `channel`,
`account`, `conversation`, `thread` and `sender_id` values into the following v2
configuration. `conversation` is the authenticated command context's raw `from`,
not a display name. `target` is the outbound destination accepted by OpenClaw;
its syntax may differ from `conversation`. Use an explicit account. Missing
identity metadata fails closed; do not substitute user-provided command text.

```yaml
messaging:
  enabled: true
  send_after: "2026-11-09T08:00:00+00:00" # Set to the actual activation time.
  provider: openclaw
  channel: telegram
  account: personal
  target: "123456789"
  conversation: "telegram:123456789"
  thread: ""
  allowed_senders: ["123456789"]
  repos: ["your-org/your-repo"]
```

Every listed sender can operate on the listed configured repositories. Choose a
private operator conversation: all members of a configured group can see outbound
questions even if they cannot respond. There is one endpoint per Robert instance.
Global controller/database alerts also go to this endpoint. The plugin and
Robert service must share the same configuration/database and run on a trusted
host. For a non-default Robert configuration, set the generated plugin's
`robertConfig` option to the absolute configuration path. OpenClaw's plugin entry
configuration is `plugins.entries.robert-openclaw.config.robertConfig`.
`robertExecutable` optionally selects the local Robert executable; it defaults to
`robert` on the gateway process PATH.

Credentials remain in OpenClaw's own channel configuration. Do not put tokens in
Robert configuration or messages. Validate Robert's configuration, then restart
the Robert daemon and supervisor so they load the new settings. Ensure `openclaw`
is on the service account's PATH and that account can send through the selected
channel. Configuration checks and mocked tests do not prove live channel access.

`send_after` is required when messaging is enabled. Use a quoted ISO timestamp
with an explicit timezone, set to the time you intentionally enable or re-enable
messaging. External delivery includes only notifications created strictly after
this time. Retain the value across ordinary service restarts so pending newer
notifications can retry. Older notices remain in the local timeline and scoped
inbox; they are neither acknowledged nor marked delivered by this filter.
The same boundary suppresses a filesystem outage notice whose failure episode
started before activation. Missing or invalid delivery boundaries withhold sending.
An explicit `send_after` also applies to custom `ROBERT_NOTIFICATION_COMMAND`
delivery, readiness checks and filesystem outage notices when OpenClaw messaging
is disabled. Legacy command-only configurations without a boundary retain their
existing behavior. Timestamp comparisons retain microsecond precision, and
publication retries preserve a notice's first creation time.

The adapter uses the documented [OpenClaw message CLI](https://docs.openclaw.ai/cli/message).
Plugin context is based on the upstream
[command types](https://github.com/openclaw/openclaw/blob/main/src/plugins/plugin-command.types.ts).
Test the real deployment with a disposable work item/question and a response
before relying on unattended delivery.

## 交付小票通知

可以让 Robert 为每项关键交付各生成一张 PNG，通过已有的 OpenClaw 目标集中发送。
在现有的 `messaging` 配置下加入 `receipts`。此功能需要同一服务账户可运行 Chromium，
并且 OpenClaw 目标渠道支持图片附件。图片沿用 Web 小票的视觉样式，摘要取自已审核结果。

```yaml
messaging:
  receipts:
    enabled: true
    send_after: "2026-11-22T09:00:00+08:00" # 改为实际启用时间
    chromium: chromium                       # 也可指定 Chrome 或 Chromium 的绝对路径
    events: [pr_opened, report_delivered]
```

- `pr_opened` 对应首次交付 PR。每个 PR 只发一次。
- `report_delivered` 对应独立分析、评审报告或投票报告完成。每个原始工作项只发一次。
- `pr_updated`、`pr_reviewed`、`pr_merged` 可按需加入，每个 PR 的每种节点各发一次。

这项配置默认关闭。第一项待发交付会等待 30 分钟，同一批交付的独立图片在一次 OpenClaw 调用中发送。
相邻两批至少间隔 30 分钟，每批最多 10 张图片。渠道决定图片显示为相册还是单独的附件。
参数变更随 daemon 重载配置生效。发送目标或仓库范围改变时，旧候选依照原目标绑定和当前授权筛选。

Robert 在数据目录的 `receipt-media` 下暂存 PNG。渠道需要本地媒体允许目录时，仅允许这一目录。
图片使用原生 OpenClaw 投递，适用于同主机且可读取暂存文件的渠道。渲染失败的批次稍后重试。
配置了 `ROBERT_NOTIFICATION_COMMAND` 时，文字走现有命令传输，小票等待原生 OpenClaw 投递恢复。
发送后结果不确定的批次保留在本地投递记录中，避免超时后重复发图。
OpenClaw 图片支持与本地路径规则见 [媒体与附件说明](https://docs.openclaw.ai/help/faq/media-and-attachments)。

## Responding

Messages show complete response commands. `/robert-inbox` retrieves pending
questions and notices, including the current version after an unrelated edit.
Messages also show the stored creation time and available repository, task title,
Issue/PR links, current task/workstream status, reason, recovery state and required
action. Sparse worker notices read these facts from the current stored task/source
and bound attempt; they do not gain new response authority. All context passes
through the existing redaction boundary. An `ack` command records receipt only.
Outbound chat explains event types, reasons, recovery states, lifecycle states
and task routes in Chinese. Internal codes remain in the JSON protocol and local
diagnostic records. An unrecognized code is shown as unclassified with a request
to inspect task details; it does not imply a cause or successful recovery.
For example:

```text
/robert-respond attention-wie-... 3 reply Use the existing SQLite database.
/robert-respond attention-wie-... 3 approve
/robert-respond attention-wie-... 3 retry
/robert-respond attention-wie-... 3 cancel
/robert-respond supervision-... 0 ack Investigating the host failure.
```

New Agent questions include two to five viable options with short tradeoffs.
The first option is marked recommended. Selecting an option submits its saved
label as the answer to that exact question. The “以上都不选，自己填写” button shows
the bound free-text reply command. In a dedicated conversation that enables
contextual replies, quote that prompt and reply with your own text.

Robert sends native command buttons through OpenClaw message presentation for
Telegram, Discord and Slack. Telegram requires inline buttons on the configured
account and destination. Reload the generated Robert plugin after upgrading.
For WeCom Bot accounts, Robert sends the complete notification followed by a
template card with full option labels, numbered buttons and a custom-answer button. Clicking
a button uses the authenticated channel callback and the existing versioned
response command, including sender, conversation and repository checks. Custom
answers display the command bound to that question.

For a display test, call the authenticated `robert.question.send` gateway method
with `preview: true` and option labels in `presentation.blocks[0].buttons`.
Preview buttons use independent test identifiers and do not require response
commands or real notification IDs. The text and card are marked as a test;
clicking any button, including the custom-answer button, only confirms the test
click and never invokes the Robert business response command.

The WeCom bridge is verified with OpenClaw `2026.10.3-t.8` and its bundled
`wecom-openclaw-plugin`. The native card transport reads that channel’s internal
connected Bot interface; it is a version-specific integration boundary. Without
that connection it uses the channel’s normal text transport. The queue saves the
body's message ID and delivery receipt before sending the card; quoted replies
use that body ID. Card receipts and retry state are stored separately. A confirmed
card rejection retries only the card under the existing attempt and time budgets.
A timeout, missing acknowledgement or interrupted card send remains `unknown`
and withholds automatic card retries until the remote effect can be reconciled.
The delivered body stays answerable in all these cases. No second transport is
tried after a card send.
`robert openclaw install --force` replaces both generated files and the
OpenClaw-managed installed copy before restarting the gateway.

Other channels can use the displayed `/robert-respond <message-id> <version> pick <number>`
or `reply` command. Older saved questions retain their text reply path.

Use only the actions offered by that message. Free-text replies preserve spaces
and newlines. With the default configuration, written replies use the explicit commands.
A changed version or resolved/replaced question is rejected. Refresh the inbox
and read the current question before issuing a new response. Duplicate identical
commands reuse the committed receipt and do not create another task. Replies,
retries and cancellations still enforce active attempt, fencing, publication and
repository restrictions. A blocked retry must be investigated through existing
local controls, not bypassed through chat.

### Plain replies in a dedicated conversation

For a conversation used exclusively for Robert notifications, enable
`messaging.contextual_replies: true` in Robert and
`plugins.entries.robert-openclaw.config.contextualReplies: true` in OpenClaw.
Both default to false. Install the current generated plugin and reload the
services after changing these settings.

Ordinary text in the authorized conversation becomes a `reply`. A quoted
Robert message selects that question using its response command, Delivery ID,
or the channel message ID stored at delivery. Without a quote, Robert selects
the most recently delivered unresolved question that accepts `reply`. Other
notifications and questions that have not been delivered do not change this
selection. The acknowledgement includes the selected notification and resulting
task IDs. There is no model interpretation of the answer or action.

An unavailable, ambiguous, already handled, or stale quoted question is rejected
without falling back to another question. If a channel omits a usable quoted
message ID and quote text, use `/robert-inbox` and the explicit response command.
Robert checks the version recorded at delivery; a later task edit requires a
fresh explicit command from the inbox. Previously sent notifications without
the new delivery receipt fields also require explicit commands.

The OpenClaw `reply_dispatch` hook uses the authenticated incoming channel,
account, raw `From`, thread and sender fields. Unrelated conversations remain
outside this handler. Slash commands keep their existing behavior. Plain text
does not approve, cancel, or retry work; use the offered command for those actions.
Attachments require explicit text controls. Incoming channel message IDs reuse
the existing atomic command receipt, so a duplicate message cannot answer a newer
question. Missing message identity withholds the reply.

Secrets are blocked or omitted; details that cannot safely be shown require local
inspection. The CLI bridge accepts JSON on stdin, not arbitrary shell commands. The plugin
rejects requests above 64 KiB before spawning and handles an early CLI exit without
crashing the OpenClaw host.
The OpenClaw plugin enforces `requireAuth` and `isAuthorizedSender`; Robert also
checks provider, channel, account, conversation/thread, sender allowlist and
repository scope. Local CLI access is a trusted host capability, not a remote
HTTP authentication mechanism. Do not expose it as an unauthenticated endpoint.

## Delivery guarantees and failures

Notifications are sent outside SQLite write transactions. Delivery updates only its
own JSON metadata member atomically, preserving concurrent operator receipts.
Repository-backed admission incidents retain their repository binding; missing
bindings are withheld from chat rather than treated as global alerts. Older
notices recover that binding from the stored incident and admission checkpoint;
controller incidents remain global. No database rewrite is needed. A stable `delivery_id`
is persisted before sending. Delivery retries at most three times within the
existing ten-minute retry window; OpenClaw calls have a 30-second timeout.
Exhausted delivery remains visible in the local notification metadata and inbox.
The protocol provides at-least-once delivery: a crash after remote send but before
receipt can duplicate a chat message. OpenClaw's message CLI has no verified
end-to-end deduplication contract here; duplicate IDs are informational, while
Robert response commands are idempotent.

When SQLite is unavailable, the supervisor retains its existing bounded
filesystem health notice and sends it through the same configured transport.
This notice asks for local database repair; a chat response cannot write to an
unavailable database. Successful recovery reconciles the health delivery record.

The existing `ROBERT_NOTIFICATION_COMMAND` transport takes precedence when set.
It receives the JSON envelope on stdin and retains its existing five-second
command timeout. Unset it to use built-in OpenClaw delivery. No transport is run
when neither option is configured.

## Future adapters (including Hermes)

`robert.message.v1` separates the transport from task and supervisor policy.
Outbound envelopes contain `notification_id`, `delivery_id`, `type`, optional
`task_id`, redacted text, choices, `version` and permitted `actions`. An inbound
request contains the authenticated provider/channel/account/conversation/thread,
`sender_id`, `notification_id`, `version`, `action` and optional `body`.
For a structured question, a `reply` can specify a one-based `choice` instead
of `body`. The controller resolves the label against the saved question after
checking its current version and scope.

A future Hermes adapter should translate authenticated host context into this
contract and implement bounded sending. It must add its provider to strict v2
validation and prove equivalent sender/session checks with contract tests. No
Hermes SDK, dynamic plugin registry, alternate task state machine or bypass of
Robert's command authority is introduced in this change. The existing command
transport can forward envelopes to other systems today; inbound providers remain
explicitly supported and fail closed until their adapter is implemented.

### Chat command output

The chat entrypoint presents Chinese summaries instead of the CLI's response
envelope. The CLI retains its JSON contract. Commands in the OpenClaw command
menu describe their purpose and supported arguments.

| Command | Default reply |
| --- | --- |
| `/robert-status` | Service record and heartbeat when available, recent task state counts, alerts and inspection commands |
| `/robert-task <task-id>` | Lifecycle, source, latest execution, failure, result and artifact commands |
| `/robert-run <run-id\|latest>` | Run status, timestamps and failed steps with reasons |
| `/robert-artifact <task-id> <artifact-type>` | Registered artifact content or a missing-file notice |
| `/robert-inbox [page <number>]` | Pending questions, choices and currently permitted versioned response commands |
| `/robert-inbox new <owner/repo> <request>` | Draft title, request ID and explicit submit command |
| `/robert-inbox <submit\|status\|retry> <request-id>` | Current dispatch state, Issue link, task command and applicable next action |
| `/robert-respond <message-id> <version> <action> [answer]` | Receipt and a command to inspect subsequent progress |

Append `--json` to status, task, run or artifact queries, or use
`/robert-inbox --json`, to request diagnostic JSON. Replies are limited to 3,500
UTF-16 code units to fit chat transports. Oversized JSON and artifact previews
identify truncation. Diagnostic JSON provides a local CLI command for the full
JSON output. Artifact queries, including local `robert artifact show ... --output json`,
read at most the last 65,536 bytes. When that preview is truncated, the reply
points to the registered file to open on the Robert host for complete content;
the JSON `data.path` field also identifies that file. If a local command or file
hint is too long to fit, the reply replaces the entire hint with a short notice;
use the configured path on the Robert host for local diagnostics and artifact lookup.
Service status is a persisted record, not a live process probe; missing status
is shown as unknown. Recent task counts describe the read model's recent tasks,
not the complete historical task population.

Inbox pages show up to three messages. Each question uses its current notification
ID, version, choices and allowed actions. Copy a displayed `pick` command to
select an option, or replace `<你的答案>` in the `reply` command. Acknowledgement
records receipt only; it does not resolve faults or authorize a retry. Accepted
responses and dispatch requests are receipts, not proof of execution or delivery.
`/robert-messaging-context` continues to return the exact JSON identity needed
for configuration.

### Query visibility in OpenClaw

`robert-status`, `robert-task`, `robert-run`, and `robert-artifact` require
OpenClaw authorization (`requireAuth` and the authenticated command context's
`isAuthorizedSender`). An authorized channel reader can query the entire Robert
instance, including registered artifacts; these four read-only commands do not
apply Robert's operator sender, session, or repository bindings. Configure the
OpenClaw channel's readers accordingly. This does not make queries anonymous.

`robert-inbox` and `robert-respond` still require Robert's configured provider,
sender, session, repository scope, and versioned command checks. Message text
never supplies authentication. Validate the installed OpenClaw version's command
context and channel authorization during deployment; generated-plugin tests do
not establish that a running gateway is configured correctly.
