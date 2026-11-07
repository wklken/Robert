# AGENTS.md

These rules apply to this directory and its descendants. Follow the
[repository dependency and state rules](../../../../AGENTS.md) here.

- Own HTTP request parsing, access checks, response encoding and rendering.
  Fetch data through `readmodels/` and send commands to domain application use
  cases. Keep business SQL and lifecycle transitions out of the interface.
- Preserve the read-only mode and write checks for Host, Origin, CSRF token and
  content type. Derive operator and repository scope from the control context.
  Preserve safe text and artifact handling in responses.
- Test response contracts and access failures at the HTTP boundary. Keep board
  query policy and state transitions in their respective domain tests.
