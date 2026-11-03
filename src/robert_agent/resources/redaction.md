# Redaction

External GitHub-facing text must be conservative.

Native review prompts require redacted output, including examples and URLs.
Before registering a merged report and its hash, Robert replaces an exact
`password=secret` literal (also with quotes or different case) with
`password=<redacted>`. This narrow normalization does not exempt other
credential literals, suffixes such as `secret!suffix`, or token patterns.
Code fences and labels such as "example" never bypass credential scanning.
Authorization and Cookie header lines whose entire value is `<redacted>` are
accepted; actual header values and placeholder suffixes remain blocked.
Local paths and internal addresses are normalized before registration too.

Submission refusals expose `redaction_blocked` or `redaction_required`, the
public action field, and safe rule/line/column locations without the matched
value. A native review content refusal keeps successful sealed model results
and enters attention; automatic recovery does not resubmit identical content.
After correcting the cause, an explicit retry can reuse verified model results.

Block publication when text contains high-risk secrets:

- tokens
- Authorization headers
- cookies
- SSH keys
- private keys
- environment secrets

Replace before publication:

- local absolute paths, including their username/path segments
- internal IP addresses
- internal domains
- temp directories
- raw command stderr when it contains host-specific context

Keep useful public context:

- GitHub URLs
- public package names
- public error types
- relative paths
- test names
- function names
