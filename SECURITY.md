# Security

## Reporting a vulnerability

Report vulnerabilities through the repository's
[private GitHub security advisory form](https://github.com/wklken/robert/security/advisories/new).
Do not open a public issue for a suspected vulnerability.

Include the affected Robert version, impact, reproduction conditions, and a
minimal redacted example. Do not include credentials, tokens, private keys,
private repository content, local diagnostics archives, or unredacted paths.

## Supported version

The current public beta receives security fixes. Older prerelease builds may
require upgrading to the latest beta.

## Security boundaries

Robert treats GitHub content as untrusted, relies on the authenticated `gh` CLI,
stores no GitHub token in YAML configuration, uses environment allowlists, and
audits proposed GitHub writes.

Git worktrees isolate code changes, not OS permissions. Run workers with access
only to the repositories and host resources you intend them to use. Worker
instructions and audit gates do not turn an unrestricted host into a sandbox.
Do not pass GitHub tokens through worker environment allowlists.

Web is local and read-only by default. Enabling writes requires an operator
identity; remote access requires an authenticated reverse proxy. CSRF protection
is not user authentication. Do not expose the server directly to the public internet.

A diagnostic export is redacted, but must still be inspected before sharing.
See the [trust model](src/robert_agent/resources/trust-model.md) and
[redaction contract](src/robert_agent/resources/redaction.md) for details.

For ordinary setup problems, use [SUPPORT.md](SUPPORT.md).
