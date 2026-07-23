# Community and Governance

## Contributing

Robert accepts issues and pull requests from the community. Keep changes
focused, preserve the trust and audit boundaries, and add behavior tests for
new logic.

### Development setup

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -e '.[dev]'
```

### Required verification

```bash
python3 -B -m unittest discover -s tests
python3 -m build
git diff --check
```

Add focused tests before the full suite. Documentation-only changes do not
require the Python suite unless they change executable examples or packaging.

### Dependencies and external actions

Explain every new dependency and why the standard library and current
dependencies are insufficient. Pull requests must disclose new network calls,
filesystem writes, subprocesses, credentials, or GitHub actions.

### DCO sign-off

Robert uses the Developer Certificate of Origin. Sign every commit:

```bash
git commit -s
```

By signing, you certify that you have the right to submit the contribution.
Robert does not require a contributor license agreement for the first beta.

## Code of Conduct

### Our Pledge

We as members, contributors, and leaders pledge to make participation in our
community a harassment-free experience for everyone, regardless of age, body
size, visible or invisible disability, ethnicity, sex characteristics, gender
identity and expression, level of experience, education, socio-economic status,
nationality, personal appearance, race, caste, color, religion, or sexual
identity and orientation.

We pledge to act and interact in ways that contribute to an open, welcoming,
diverse, inclusive, and healthy community.

### Our Standards

Examples of behavior that contributes to a positive environment include:

- Demonstrating empathy and kindness toward other people.
- Being respectful of differing opinions, viewpoints, and experiences.
- Giving and gracefully accepting constructive feedback.
- Accepting responsibility, apologizing, and learning from mistakes.
- Focusing on what is best for the overall community.

Examples of unacceptable behavior include:

- Sexualized language or imagery and sexual attention or advances.
- Trolling, insulting or derogatory comments, and personal or political attacks.
- Public or private harassment.
- Publishing another person's private information without permission.
- Conduct that is inappropriate in a professional setting.

### Enforcement Responsibilities

Community leaders are responsible for clarifying and enforcing these standards
and may remove, edit, or reject contributions that are not aligned with them.
Reasons for moderation decisions will be communicated when appropriate.

### Scope

This Code of Conduct applies in all community spaces and when an individual is
officially representing the community in public spaces.

### Enforcement

Report unacceptable behavior privately through the repository's
[GitHub security advisory form](https://github.com/wklken/robert/security/advisories/new).
All reports will be reviewed promptly and fairly. Community leaders must
respect the privacy and security of reporters.

### Enforcement Guidelines

1. **Correction** — A private written warning and clarification. A public
   apology may be requested.
2. **Warning** — A warning with consequences and a temporary restriction on
   interaction.
3. **Temporary Ban** — A temporary ban from community interaction.
4. **Permanent Ban** — A permanent ban for sustained or severe violations.

This Code of Conduct is adapted from the
[Contributor Covenant 2.1](https://www.contributor-covenant.org/version/2/1/code_of_conduct.html).

## Security

### Reporting a vulnerability

Report vulnerabilities through the repository's
[private GitHub security advisory form](https://github.com/wklken/robert/security/advisories/new).
Do not open a public issue for a suspected vulnerability.

Include the affected Robert version, impact, reproduction conditions, and a
minimal redacted example. Do not include credentials, tokens, private keys,
private repository content, local diagnostics archives, or unredacted paths.

### Supported version

The current public beta receives security fixes. Older prerelease builds may
require upgrading to the latest beta.

### Security boundaries

Robert treats GitHub content as untrusted, relies on the authenticated `gh` CLI,
stores no GitHub token in YAML configuration, isolates worker directories, uses
environment allowlists, and audits all proposed GitHub writes.

## Support

Use the GitHub issue forms for reproducible bugs, feature requests, and
documentation problems. Include the Robert version, operating system, Python
version, and redacted `robert doctor --output json` output.

Use [GitHub Discussions](https://github.com/wklken/robert/discussions) for
setup questions and general usage help. Security reports belong in private
GitHub security advisories.

Before posting, remove credentials, private repository content, internal URLs,
local paths, worker logs, and sensitive diagnostic data.
