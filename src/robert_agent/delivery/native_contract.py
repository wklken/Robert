"""Validate native Worker candidates against a frozen repository task binding."""

from copy import deepcopy
import json
import re

from robert_agent.context.memory_policy import redact_memory_delta
from robert_agent.delivery import contract, policies
from robert_agent.foundation.contracts.delivery import GitHubActionType, OutputType
from robert_agent.security.redaction import redact_text

_COMMENT = ("tapd", GitHubActionType.COMMENT.value)
_PUSH = ("github", GitHubActionType.PUSH_EXISTING_PR.value)
_OPEN = ("github", GitHubActionType.OPEN_PR.value)
_ACTION_SEQUENCE = {
    OutputType.LOCAL_RESULT: (),
    OutputType.CLASSIFICATION_RESULT: (),
    OutputType.COMMENT_ANALYSIS: (_COMMENT,),
    OutputType.WAITING_FOR_USER: (_COMMENT,),
    OutputType.NEW_PR: (_PUSH, _OPEN, _COMMENT),
    OutputType.UPDATE_EXISTING_PR: (_PUSH, _COMMENT),
}
_ROLES = {
    "evidence": {OutputType.LOCAL_RESULT},
    "classification": {OutputType.CLASSIFICATION_RESULT},
    "answer": {OutputType.COMMENT_ANALYSIS, OutputType.WAITING_FOR_USER},
    "development": {OutputType.NEW_PR, OutputType.UPDATE_EXISTING_PR},
}
_ROUTE_BY_OUTPUT = {
    OutputType.LOCAL_RESULT: "source-evidence",
    OutputType.CLASSIFICATION_RESULT: "classification-result",
    OutputType.COMMENT_ANALYSIS: "comment-analysis",
    OutputType.WAITING_FOR_USER: "waiting-for-user",
    OutputType.NEW_PR: "new-pr",
    OutputType.UPDATE_EXISTING_PR: "update-existing-pr",
}
_IDENTITY = ("round_id", "request_id", "repo_id", "capability")
_SELECTOR = frozenset({"provider", "type", "target_id"})
_EVIDENCE_PATH = re.compile(r"[A-Za-z0-9._/-]{1,240}")
_EVIDENCE_REVISION = re.compile(r"sha256:[0-9a-f]{64}")


def _require(condition, field, message, code="invalid_submission"):
    if not condition:
        raise contract.InvalidSubmission(field, message, code)


def route_id_for(output):
    return _ROUTE_BY_OUTPUT.get(output)


def selectors_for(output):
    """Return prompt selectors for a fixed controller-selected output."""
    return [
        {"provider": provider, "type": action, "target_id": "ticket" if provider == "tapd" else "repository"}
        for provider, action in _ACTION_SEQUENCE[output]
    ]


def _key(action):
    return action["provider"], action["type"]


def binding(metadata, row, *, expected_output, route_actions, route_id):
    """Resolve controller selectors into an immutable snapshot of actual targets."""
    manifest = metadata.get("native_result")
    if (
        not isinstance(manifest, dict)
        or set(manifest) != {*_IDENTITY, "role", "allowed_actions"}
        or not row
        or tuple(manifest[name] for name in _IDENTITY) != tuple(row[name] for name in _IDENTITY)
        or row["status"] != "active"
        or row["capability"] not in {"readonly", "development"}
        or (row["capability"] == "development" and row["round_capability"] != "development")
        or row["repo_id"] not in json.loads(row["repo_ids_json"])
        or row["route_id"] != route_id
        or _ROUTE_BY_OUTPUT.get(expected_output) != route_id
    ):
        return None
    role = manifest["role"]
    if (
        not isinstance(role, str)
        or expected_output not in _ROLES.get(role, ())
        or (role == "development" and row["capability"] != "development")
        or (role == "evidence" and row["capability"] != "readonly")
    ):
        return None
    ticket = {
        "provider": "tapd",
        "host": row["host"],
        "workspace_id": row["project_key"],
        "kind": row["source_type"],
        "external_id": row["external_id"],
    }
    if json.loads(row["publication_scope_json"]) != {**ticket, "action": "comment"}:
        return None
    required = set(_ACTION_SEQUENCE[expected_output])
    # A development attempt may stop for a TAPD clarification.
    if role in {"answer", "development"}:
        required.add(_COMMENT)
    allowed = manifest["allowed_actions"]
    if not isinstance(allowed, list) or len(allowed) != len(required):
        return None
    targets = {}
    keys = set()
    for action in allowed:
        if (
            not isinstance(action, dict)
            or set(action) != _SELECTOR
            or not all(isinstance(value, str) for value in action.values())
            or _key(action) not in required
            or action["type"] not in route_actions
            or _key(action) in keys
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", action["target_id"])
        ):
            return None
        keys.add(_key(action))
        if action["provider"] == "tapd":
            target = ticket
        else:
            target = {
                "provider": "github",
                "host": "github.com",
                "repository": row["full_name"],
                "repo_id": row["repo_id"],
                "kind": "repository",
            }
            if expected_output == OutputType.UPDATE_EXISTING_PR:
                if (
                    row["primary_provider"] != "github"
                    or row["primary_host"] != target["host"]
                    or row["primary_project"].casefold() != target["repository"].casefold()
                    or row["primary_kind"] != "pull_request"
                ):
                    return None
                target = {**target, "kind": "pull_request", "external_id": row["primary_external_id"]}
            elif row["primary_provider"] != "tapd":
                return None
        targets[(action["target_id"], *(_key(action)))] = target
    recommendations = classification_routes(row["capability"], existing_pr=row["primary_provider"] == "github")
    return {
        "source": {
            **ticket,
            "source_id": row["source_id"],
            "round_input_sha256": row["input_sha256"],
            **{name: manifest[name] for name in _IDENTITY},
            "role": role,
        },
        "targets": targets,
        "expected_output": expected_output,
        "classification_routes": recommendations if role == "classification" else [],
    }


def classification_routes(capability, *, existing_pr=False):
    routes = ["comment-analysis", "waiting-for-user"]
    if capability == "development":
        routes.insert(0, "update-existing-pr" if existing_pr else "new-pr")
    return routes


def _evidence(value, repo_id):
    _require(isinstance(value, list) and 0 < len(value) <= 32, "evidence", "Provide 1 to 32 evidence references.")
    paths = set()
    for item in value:
        _require(
            isinstance(item, dict)
            and set(item) == {"repo_id", "path", "revision"}
            and item["repo_id"] == repo_id
            and isinstance(item["path"], str)
            and _EVIDENCE_PATH.fullmatch(item["path"])
            and all(part not in {"", ".", "..", ".git"} for part in item["path"].split("/"))
            and item["path"] not in paths
            and isinstance(item["revision"], str)
            and _EVIDENCE_REVISION.fullmatch(item["revision"]),
            "evidence",
            "Evidence must name a unique repository-relative path and its exact byte digest in the assigned repository.",
        )
        paths.add(item["path"])


def _check_output(output_type, native):
    role = native["source"]["role"]
    expected = native["expected_output"]
    permitted = {expected}
    if role in {"answer", "development"}:
        permitted.add(OutputType.WAITING_FOR_USER)
    _require(output_type in permitted, "output_type", "Output is outside the bound native role and route.")


def _check_actions(actions, output_type, native, *, normalized):
    _require(
        isinstance(actions, list) and len(actions) <= 3, "planned_actions", "Provide the actions for the bound output."
    )
    sequence = []
    bound = []
    for index, action in enumerate(actions):
        field = f"planned_actions[{index}]"
        _require(isinstance(action, dict), field, "Action must be an object.")
        _require(
            all(isinstance(action.get(key), str) for key in _SELECTOR), field, "Select a controller-approved action."
        )
        provider, kind = _key(action)
        target = native["targets"].get((action["target_id"], provider, kind))
        _require(target is not None, field, "Action target is outside the bound scope.", "action_scope")
        fields = _SELECTOR | ({"body"} if provider == "tapd" else set())
        if normalized:
            fields = fields | {"target"}
        _require(set(action) == fields, field, "Action contains missing or program-owned fields.")
        if normalized:
            _require(action["target"] == target, field, "Stored target differs from the bound target.", "action_scope")
        if provider == "tapd":
            body = action["body"]
            _require(
                isinstance(body, str) and 0 < len(body.strip()) <= 12000 and not contract.CONTROL_MARKER.search(body),
                field + ".body",
                "Provide TAPD reply text without controller markers.",
            )
        sequence.append((provider, kind))
        bound.append({**action, "target": target})
    _require(
        tuple(sequence) == _ACTION_SEQUENCE[output_type],
        "planned_actions",
        "Actions must match the bound output and publication order.",
    )
    return bound


def _check_question(payload):
    waiting = payload["output_type"] == OutputType.WAITING_FOR_USER
    _require(
        waiting == (payload.get("operator_question") is not None),
        "operator_question",
        "An operator question is required for a waiting result and forbidden for other results.",
    )
    if waiting:
        error = policies.validate_operator_question(payload)
        _require(error is None, "operator_question", error or "Invalid operator question.")


def normalize(envelope, native):
    """Bind native action selectors. GitHub actions contain no Worker-provided text."""
    submission = envelope["submission"]
    output_type = envelope["output_type"]
    _require(isinstance(submission, dict), "result", "Submission must be an object.")
    _check_output(output_type, native)
    fields = (contract.FIELDS - {"planned_github_actions", "branch_slug"}) | {
        "planned_actions",
        "evidence",
        "investigation_question",
    }
    _require(
        not set(submission) - fields,
        "result",
        "Submission contains unsupported or program-owned fields.",
        "program_owned_field",
    )
    # Reuse the shared semantic validation without projecting TAPD into a GitHub action.
    common = {
        key: value
        for key, value in submission.items()
        if key not in {"planned_actions", "evidence", "investigation_question"}
    }
    contract.validate_submission(common, output_type, "tapd", native=True)
    result = deepcopy(submission)
    if "memory_delta" in result:
        result["memory_delta"] = redact_memory_delta(result["memory_delta"])
    result.update({key: envelope[key] for key in ("task_id", "attempt_id", "output_type")})
    result["native_source"] = native["source"]
    result["planned_actions"] = _check_actions(submission.get("planned_actions"), output_type, native, normalized=False)
    result.setdefault("consumed_work_item_event_ids", [])
    validate_bound_result(result, native)
    return result


def validate_bound_result(result, native):
    """Recheck the stored candidate at ingest and final audit."""
    output_type = result["output_type"]
    _check_output(output_type, native)
    _require(
        result.get("native_source") == native["source"] and "planned_github_actions" not in result,
        "result",
        "Saved result binding differs from the current attempt.",
        "action_scope",
    )
    _check_actions(result.get("planned_actions"), output_type, native, normalized=True)
    _check_question(result)
    fingerprints = result.get("consumed_event_fingerprints")
    _require(
        isinstance(fingerprints, list)
        and bool(fingerprints)
        and all(isinstance(value, str) and value in native["event_fingerprints"] for value in fingerprints)
        and len(fingerprints) == len(set(fingerprints)),
        "consumed_event_fingerprints",
        "Name handled events from the bound request.",
    )
    _require(
        not result.get("consumed_work_item_event_ids"),
        "consumed_work_item_event_ids",
        "Native results use the bound source event fingerprints.",
    )
    recommendation = result.get("recommended_route")
    if output_type == OutputType.CLASSIFICATION_RESULT:
        _require(
            isinstance(recommendation, str) and recommendation in native["classification_routes"],
            "recommended_route",
            "Select a route within the bound native capability and source.",
        )
    else:
        _require(not recommendation, "recommended_route", "Route recommendations belong to classification results.")
    if output_type == OutputType.LOCAL_RESULT and "investigation_question" in result:
        question = result["investigation_question"]
        _require(
            isinstance(question, str)
            and bool(question.strip())
            and len(question) <= 700
            and "\0" not in question
            and result.get("evidence", []) == []
            and not contract.CONTROL_MARKER.search(question)
            and redact_text(question, strict_assignments=True).get("text") == question,
            "investigation_question",
            "Provide one safe clarification question with no evidence references.",
        )
    elif output_type == OutputType.LOCAL_RESULT:
        _evidence(result.get("evidence"), native["source"]["repo_id"])
    else:
        _require(
            "evidence" not in result and "investigation_question" not in result,
            "evidence",
            "Investigation outputs belong to LOCAL_RESULT.",
        )
