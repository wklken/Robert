"""Parse repository identity from an already configured Git remote."""

from urllib.parse import urlparse


def github_owner(remote_url):
    remote_url = (remote_url or "").strip()
    if not remote_url:
        return ""
    if remote_url.startswith("git@") and ":" in remote_url:
        path = remote_url.split(":", 1)[1]
    else:
        path = urlparse(remote_url).path.lstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    parts = [part for part in path.split("/") if part]
    return parts[0] if len(parts) >= 2 else ""


def github_repository(remote_url):
    """Return a GitHub.com repository identity, never infer it from a local path."""
    value = (remote_url or "").strip()
    if value.startswith("git@github.com:"):
        path = value[len("git@github.com:") :]
    else:
        parsed = urlparse(value)
        if (
            parsed.hostname != "github.com"
            or parsed.scheme not in {"https", "ssh"}
            or parsed.password is not None
            or (parsed.username is not None and (parsed.scheme != "ssh" or parsed.username != "git"))
        ):
            return ""
        path = parsed.path.lstrip("/")
    path = path.removesuffix(".git").rstrip("/")
    parts = path.split("/")
    return "/".join(parts).casefold() if len(parts) == 2 and all(parts) else ""
