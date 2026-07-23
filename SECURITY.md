# Security Policy

## Reporting a vulnerability

Report vulnerabilities through the repository's private GitHub security
advisory feature. Do not open a public issue for a suspected vulnerability.

Include the affected Robert version, impact, reproduction conditions, and a
minimal redacted example. Do not include credentials, tokens, private keys,
private repository content, local diagnostics archives, or unredacted paths.

## Supported version

The current public beta receives security fixes. Older prerelease builds may
require upgrading to the latest beta.

## Security boundaries

Robert treats GitHub content as untrusted, relies on the authenticated `gh` CLI,
stores no GitHub token in its YAML configuration, isolates worker directories,
uses environment allowlists, and audits all proposed GitHub writes.
