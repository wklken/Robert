"""Pure serialization of durable native review supersession receipts."""

import json


def encode_round_stop(reason):
    """A controller stop keeps its readable message and exact recovery facts."""
    return json.dumps({"round_stop_version": 1, **reason}, ensure_ascii=False, sort_keys=True)


def decode_round_stop(error):
    """Decode only this explicit envelope; ordinary errors remain ordinary text."""
    if not isinstance(error, str) or not error.startswith('{"') or '"round_stop_version"' not in error:
        return None
    try:
        value = json.loads(error)
    except ValueError:
        return None
    if (
        not isinstance(value, dict)
        or value.get("round_stop_version") != 1
        or value.get("reason_code") != "review_revision_changed"
        or not all(
            isinstance(value.get(key), str) and value[key]
            for key in ("safe_error", "round_id", "task_id", "attempt_id", "canonical_sha256")
        )
    ):
        return None
    return value
