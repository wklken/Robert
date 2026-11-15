"""Independent bounded remote proof for one frozen native GitHub request."""

from hashlib import sha256
import json
import os
import time
import re
from urllib.parse import quote, urlencode, urlsplit

from robert_agent.foundation.commands import ControlDeadlineExceeded
from robert_agent.foundation.contracts.delivery import GitHubActionType
from robert_agent.persistence.queries.source_github_publication import canonical_remote_proof

_PER_PAGE = 50
_MAX_PAGES = 4
_MAX_RESPONSE = 2 * 1024 * 1024


def _api(run, endpoint):
    result = run(["gh", "api", "--hostname", "github.com", endpoint], capture_output=True, text=True)
    if result.returncode or not isinstance(result.stdout, str) or len(result.stdout) > _MAX_RESPONSE:
        raise ValueError("Native GitHub read unavailable")
    return json.loads(result.stdout)


def _branch(run, effect):
    ref = f"refs/heads/{effect['branch']}"
    endpoint = f"repos/{effect['head_repository']}/git/matching-refs/heads/{quote(effect['branch'], safe='')}"
    rows = _api(run, endpoint)
    if not isinstance(rows, list) or len(rows) > _PER_PAGE or any(not isinstance(row, dict) for row in rows):
        raise ValueError("Invalid native GitHub ref response")
    exact = [row for row in rows if row.get("ref") == ref]
    if not exact:
        return None
    obj = exact[0].get("object")
    if (
        len(exact) != 1
        or not isinstance(obj, dict)
        or obj.get("type") != "commit"
        or not isinstance(obj.get("sha"), str)
        or not re.fullmatch(r"[0-9a-f]{40}", obj["sha"])
    ):
        raise ValueError("Invalid native GitHub ref proof")
    return {"repository": effect["head_repository"], "ref": ref, "sha": obj["sha"]}


def read_base_reference(run, repository, branch, *, stop=None):
    """Read one exact upstream base ref for accepted-result recovery."""

    def bounded(command, **kwargs):
        timeout = min(10, getattr(stop, "deadline", float("inf")) - time.monotonic())
        if timeout <= 0 or (stop is not None and stop.is_set()):
            raise ControlDeadlineExceeded(command, 0)
        environment = {key: value for key, value in os.environ.items() if key not in {"GH_HOST", "GH_REPO"}}
        environment.update(GH_HOST="github.com", GH_PROMPT_DISABLED="1")
        return run(command, timeout=timeout, env=environment, **kwargs)

    return _branch(bounded, {"head_repository": repository, "branch": branch})


def _canonical_pr_url(value, repository, number):
    if not isinstance(value, str) or type(number) is not int or number <= 0:
        return None
    parsed = urlsplit(value)
    path = parsed.path.split("/")
    if (
        parsed.scheme != "https"
        or parsed.netloc.casefold() != "github.com"
        or parsed.query
        or parsed.fragment
        or len(path) != 5
        or path[0] != ""
        or "/".join(path[1:3]).casefold() != repository.casefold()
        or path[3:] != ["pull", str(number)]
    ):
        return None
    return f"https://github.com/{repository}/pull/{number}"


def _pr_fields(item, packet, *, sha, body):
    effect, identity = packet["effect"], packet["identity"]
    head, base = item["head"], item["base"]
    number = item["number"]
    state = "merged" if item.get("merged") is True or item.get("merged_at") else item.get("state")
    url = _canonical_pr_url(item["html_url"], identity["repository"], number)
    proof = {
        "number": number,
        "url": url,
        "author": item["user"]["login"].casefold(),
        "head_repository": head["repo"]["full_name"].casefold(),
        "head_branch": head["ref"],
        "head_sha": head["sha"],
        "base_repository": base["repo"]["full_name"].casefold(),
        "base_branch": base["ref"],
        "state": state,
    }
    if (
        url is None
        or state not in ("open", "closed", "merged")
        or any(
            proof[key] != expected
            for key, expected in {
                "author": packet["publisher"],
                "head_repository": effect["head_repository"],
                "head_branch": effect["branch"],
                "head_sha": sha,
                "base_repository": identity["repository"].casefold(),
                "base_branch": effect["base"],
            }.items()
        )
    ):
        return None
    if body:
        actual = item["body"].replace("\r\n", "\n")
        if item["title"] != effect["title"] or actual != effect["body"] or actual.count(packet["marker"]) != 1:
            return None
        proof.update(title=item["title"], body_sha256=sha256(actual.encode()).hexdigest(), marker=packet["marker"])
    return proof


def _all_head_pulls(run, packet):
    effect, identity = packet["effect"], packet["identity"]
    owner = effect["head_repository"].split("/", 1)[0]
    seen = set()
    pulls = []
    for page in range(1, _MAX_PAGES + 1):
        params = urlencode({"state": "all", "head": f"{owner}:{effect['branch']}", "per_page": _PER_PAGE, "page": page})
        rows = _api(run, f"repos/{identity['repository']}/pulls?{params}")
        if not isinstance(rows, list) or len(rows) > _PER_PAGE:
            raise ValueError("Invalid native GitHub PR page")
        if not rows:
            return pulls
        for item in rows:
            number = item["number"]
            if type(number) is not int or number <= 0 or number in seen:
                raise ValueError("Unstable native GitHub PR page")
            seen.add(number)
            pulls.append(item)
    raise ValueError("Incomplete native GitHub PR listing")


def _matching_open_action(run, packet):
    rows = _all_head_pulls(run, packet)
    if len(rows) != 1:
        return None
    return _pr_fields(rows[0], packet, sha=packet["effect"]["head_sha"], body=True)


def _saved_pr(run, packet):
    return _api(run, f"repos/{packet['identity']['repository']}/pulls/{packet['effect']['pr_number']}")


def observe_effect(run, packet):
    """Return one canonical positive proof; missing or divergent remote facts stay unknown."""
    effect, identity = packet["effect"], packet["identity"]
    if identity["action_type"] == GitHubActionType.OPEN_PR:
        remote = _matching_open_action(run, packet)
        proof = {"pr": remote}
    else:
        proof = {"branch": _branch(run, effect)}
        if effect.get("pr_number"):
            remote = _saved_pr(run, packet)
            proof["pr"] = _pr_fields(remote, packet, sha=effect["head_sha"], body=False)
    return proof if canonical_remote_proof(packet, proof) else None


def may_start_effect(run, packet):
    """Only an exact expected starting state and authenticated publisher may write."""
    effect, identity = packet["effect"], packet["identity"]
    actor = _api(run, "user")
    login = actor.get("login") if isinstance(actor, dict) else None
    if not isinstance(login, str) or login.casefold() != packet["publisher"]:
        return False
    branch = _branch(run, effect)
    if identity["action_type"] == GitHubActionType.OPEN_PR:
        return bool(branch and branch["sha"] == effect["head_sha"] and not _all_head_pulls(run, packet))
    if not effect.get("pr_number"):
        return branch is None
    item = _saved_pr(run, packet)
    initial = effect["expected_remote_sha"]
    pr = _pr_fields(item, packet, sha=initial, body=False)
    return bool(
        branch
        and branch["sha"] == initial
        and pr
        and pr["state"] == "open"
        and item["number"] == effect["pr_number"]
        and item["base"]["sha"] == effect["base_sha"]
    )
