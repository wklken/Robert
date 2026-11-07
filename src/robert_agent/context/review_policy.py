"""Shared judgment policy for source review and review-claim verification."""

import json

from robert_agent.resource_files import resource


def guidance(github_account=""):
    return (resource("review-policy.md").read_text(encoding="utf-8").strip()
            + "\nConfigured Robert GitHub account: " + json.dumps(github_account) + ".\n")
