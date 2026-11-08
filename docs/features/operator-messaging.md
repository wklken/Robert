# Operator messaging

[文档目录](../README.md) · [功能专题](README.md)

Robert can send worker questions, approval requests and failure notifications to
OpenClaw and receive explicit responses from an authorized operator. Messaging
is disabled by default. The existing status/artifact commands remain available.

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
missing database. No new database table or runtime dependency is required. Disabling messaging
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

Credentials remain in OpenClaw's own channel configuration. Do not put tokens in
Robert configuration or messages. Validate Robert's configuration, then restart
the Robert daemon and supervisor so they load the new settings. Ensure `openclaw`
is on the service account's PATH and that account can send through the selected
channel. Configuration checks and mocked tests do not prove live channel access.

The adapter uses the documented [OpenClaw message CLI](https://docs.openclaw.ai/cli/message).
Plugin context is based on the upstream
[command types](https://github.com/openclaw/openclaw/blob/main/src/plugins/plugin-command.types.ts).
Test the real deployment with a disposable work item/question and a response
before relying on unattended delivery.

## Responding

Messages show complete response commands. `/robert-inbox` retrieves pending
questions and notices, including the current version after an unrelated edit.
For example:

```text
/robert-respond attention-wie-... 3 reply Use the existing SQLite database.
/robert-respond attention-wie-... 3 approve
/robert-respond attention-wie-... 3 retry
/robert-respond attention-wie-... 3 cancel
/robert-respond supervision-... 0 ack Investigating the host failure.
```

Use only the actions offered by that message. Free-text replies preserve spaces
and newlines. Choice IDs/labels are suggestions; answers can explain a choice in
text. There is no implicit “latest question” or natural-language authorization.
A changed version or resolved/replaced question is rejected. Refresh the inbox
and read the current question before issuing a new response. Duplicate identical
commands reuse the committed receipt and do not create another task. Replies,
retries and cancellations still enforce active attempt, fencing, publication and
repository restrictions. A blocked retry must be investigated through existing
local controls, not bypassed through chat.

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

A future Hermes adapter should translate authenticated host context into this
contract and implement bounded sending. It must add its provider to strict v2
validation and prove equivalent sender/session checks with contract tests. No
Hermes SDK, dynamic plugin registry, alternate task state machine or bypass of
Robert's command authority is introduced in this change. The existing command
transport can forward envelopes to other systems today; inbound providers remain
explicitly supported and fail closed until their adapter is implemented.
