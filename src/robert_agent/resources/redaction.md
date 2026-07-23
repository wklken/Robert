# Redaction

External GitHub-facing text must be conservative.

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
