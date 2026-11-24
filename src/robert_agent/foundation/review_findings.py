"""Stable identity and follow-up signals in the deterministic review protocol."""

import hashlib
import json


def deterministic_finding_id(finding):
    location = finding["location"]
    claim = [
        location["path"],
        location["line"],
        location.get("end_line", location["line"]),
        *(finding[key] for key in ("title", "trigger", "impact")),
    ]
    encoded = json.dumps(claim, ensure_ascii=False).encode()
    return "F-" + hashlib.sha256(encoded).hexdigest()[:32]


def review_requires_followup(result):
    return (
        result["requires_judgment"] is not False
        or result["merge_recommendation"] != "ready to merge"
        or bool(result["findings"] or result["dismissed_findings"])
    )
