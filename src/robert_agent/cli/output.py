import json
import sys
from typing import TextIO


def emit_result(
    result: dict,
    output: str = "text",
    stream: TextIO | None = None,
) -> None:
    target = stream or sys.stdout
    if output == "json":
        print(json.dumps(result, ensure_ascii=False, sort_keys=True), file=target)
        return
    message = result.get("message") or result.get("status") or ""
    print(str(message), file=target)
    for key in sorted(result):
        if key in {"message", "status", "ok"}:
            continue
        print(f"{key}: {result[key]}", file=target)
