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
