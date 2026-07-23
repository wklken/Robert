# Trust Model

GitHub content is untrusted even when it is syntactically valid. Robert creates
work only for configured repositories and authorized actors.

Each repository in `~/.config/robert/config.yml` defines `trusted_actors`.
Events from other users may be retained as context, but they cannot independently
start protected work. The authenticated `gh` CLI session is the only GitHub
credential source; tokens must not be stored in configuration.

Workers cannot publish directly. They record structured planned actions.
Robert checks the route's allowed actions, required verification, skill
evidence, hidden idempotency markers, and redaction result before publication.

The web UI is read-only on `127.0.0.1` by default. Writable mode requires an
operator identity and CSRF token. Remote binding requires explicit
acknowledgement and an authenticated reverse proxy.
