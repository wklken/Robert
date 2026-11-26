"""Pure recipient selection shared by PR review prompts and publication."""

import re


def decision_owner(request_owner, pr_author_login, github_account):
    """Address a human PR author; robot-authored PRs retain their request owner."""
    if not isinstance(pr_author_login, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", pr_author_login):
        return None
    if pr_author_login.casefold() == "github":
        return None
    if github_account and pr_author_login.casefold() == github_account.casefold():
        return request_owner
    return {"login": pr_author_login}
