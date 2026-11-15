"""Build durable identity fields from observed source coordinates."""

from urllib.parse import urlsplit


def github_source_host(project_key, source_type, number, html_url):
    if not html_url:
        return "github.com"
    try:
        url = urlsplit(html_url)
        marker = "issues" if source_type == "issue" else "pull"
        if (
            url.scheme in {"http", "https"}
            and url.hostname
            and not (url.username or url.password)
            and url.path.rstrip("/").casefold() == f"/{project_key}/{marker}/{number}".casefold()
        ):
            return url.hostname
    except ValueError:
        pass
    raise ValueError("source URL does not establish a GitHub host for its repository and number")
