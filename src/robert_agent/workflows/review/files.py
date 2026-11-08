"""Model-independent file contracts for native PR review.

The controller supplies trusted paths and process identities. Model documents are
untrusted data; no field here grants publication or execution authority.
"""

import hashlib
import json
import os
import re
from pathlib import Path, PurePosixPath
import tempfile


SCHEMA_VERSION = 1
SEVERITIES = ("critical", "high", "medium", "low")
RECOMMENDATIONS = ("ready to merge", "merge after fixes", "needs significant rework")
DISPOSITIONS = ("confirmed", "dismissed", "needs_confirmation")


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def safe_path(path, root):
    """Reject links/escapes inside an already trusted artifact directory."""
    root = Path(root).resolve()
    path = Path(os.path.abspath(path))
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ValueError("review artifact is outside its registered directory") from exc
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("review artifact path contains a symlink")
    if path.resolve() != path:
        raise ValueError("review artifact path escapes its registered directory")
    return path


def atomic_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError("cannot replace a symlink artifact")
    # A unique temporary name also makes controller writes safe across retries.
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _read_json(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid review JSON: {path.name}") from exc
    if not isinstance(value, dict):
        raise ValueError("review JSON must be an object")
    return value


def _text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be nonempty text")
    return value


def _source_path(value):
    _text(value, "source path")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("source location contains control characters")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "\\" in value or value in (".", ""):
        raise ValueError("source location must be a relative repository path")
    return value


def _identity(document, manifest):
    if type(document.get("schema_version")) is not int or document["schema_version"] != SCHEMA_VERSION:
        raise ValueError("unsupported review schema_version")
    for key in ("round_id", "snapshot_id", "head_sha"):
        if document.get(key) != manifest.get(key) or not manifest.get(key):
            raise ValueError(f"review {key} does not match the registered snapshot")


def _finding(finding, dismissed=False):
    if not isinstance(finding, dict):
        raise ValueError("finding must be an object")
    for key in ("id", "title", "trigger", "impact", "evidence", "suggestion"):
        _text(finding.get(key), f"finding.{key}")
    if finding.get("severity") not in SEVERITIES:
        raise ValueError("finding severity must be critical/high/medium/low")
    location = finding.get("location")
    if not isinstance(location, dict):
        raise ValueError("finding requires a source location")
    _source_path(location.get("path"))
    line = location.get("line")
    if type(line) is not int or line < 1:
        raise ValueError("source line must be a positive integer")
    if "end_line" in location and (type(location["end_line"]) is not int or location["end_line"] < line):
        raise ValueError("source end_line must not precede line")
    if dismissed:
        _text(finding.get("reason"), "dismissed finding reason")


def read_result(path, manifest):
    path = safe_path(path, Path(manifest["artifact_root"]) / "runs")
    if path.name != "result.json":
        raise ValueError("only the committed result.json is accepted")
    document = _read_json(path)
    _identity(document, manifest)
    seen = set()
    for key in ("findings", "dismissed_findings"):
        if not isinstance(document.get(key), list):
            raise ValueError(f"{key} must be an array")
        for finding in document[key]:
            _finding(finding, dismissed=key == "dismissed_findings")
            if finding["id"] in seen:
                raise ValueError("finding ids must be unique within a reviewer result")
            seen.add(finding["id"])
    coverage = document.get("coverage")
    if not isinstance(coverage, dict):
        raise ValueError("coverage must be an object")
    for key in ("reviewed_paths", "unreviewed_paths"):
        if not isinstance(coverage.get(key), list):
            raise ValueError(f"coverage.{key} must be an array")
        for value in coverage[key]:
            _source_path(value)
    _text(coverage.get("notes"), "coverage.notes")
    if document.get("merge_recommendation") not in RECOMMENDATIONS:
        raise ValueError("invalid merge recommendation")
    return document


def _code_span(text):
    # CommonMark closes a code span only with a run matching its opener.
    fence = "`" * (1 + max((len(run) for run in re.findall(r"`+", text)), default=0))
    content = f" {text} " if "`" in text else text
    return f"{fence}{content}{fence}"


def _location(finding):
    location = finding["location"]
    return f"{location['path']}:{location['line']}"


def _finding_lines(finding):
    return [
        f"- **[{finding['severity'].upper()}] {finding['title']}** — {_code_span(_location(finding))}",
        f"  - 触发：{finding['trigger']}",
        f"  - 影响：{finding['impact']}",
        f"  - 证据：{finding['evidence']}",
        f"  - 建议：{finding['suggestion']}",
    ]


def render_individual(result, worker_id, model):
    lines = [f"# {worker_id} Review ({model})", "", f"快照：{result['head_sha']}", ""]
    for severity in SEVERITIES:
        lines.extend([f"## {severity.title()} Issues", ""])
        findings = [item for item in result["findings"] if item["severity"] == severity]
        for finding in findings:
            lines.extend(_finding_lines(finding))
        if not findings:
            lines.append("无")
        lines.append("")
    lines.extend(["## Dismissed Findings", ""])
    for finding in result["dismissed_findings"]:
        lines.extend(_finding_lines(finding))
        lines.append(f"  - 反证：{finding['reason']}")
    if not result["dismissed_findings"]:
        lines.append("无")
    coverage = result["coverage"]
    coverage_lines = [json.dumps(coverage, ensure_ascii=False)]
    # Existing reports are byte-checked during resume. Fence only paths that
    # the previous validator could not accept, preserving all older artifacts.
    if any("`" in path for key in ("reviewed_paths", "unreviewed_paths") for path in coverage[key]):
        coverage_lines = ["```json", *coverage_lines, "```"]
    lines.extend(
        [
            "",
            "## Coverage",
            "",
            *coverage_lines,
            "",
            f"**合并建议**：{result['merge_recommendation']}",
            "",
        ]
    )
    return "\n".join(lines)


def _validated_records(manifest, records):
    if not isinstance(records, list) or not records:
        raise ValueError("synthesis requires all selected reviewer records")
    workers = set()
    documents = {}
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("reviewer record must be an object")
        worker_id = _text(record.get("worker_id"), "worker_id")
        if worker_id in workers:
            raise ValueError("synthesis may select only one result per worker")
        workers.add(worker_id)
        for key in ("model", "run_id"):
            _text(record.get(key), key)
        for kind in ("result", "report"):
            path = safe_path(record.get(f"{kind}_path", ""), Path(manifest["artifact_root"]) / "runs")
            if path.name != f"{kind}.json" and not (kind == "report" and path.name == "report.md"):
                raise ValueError("unexpected selected artifact filename")
            try:
                digest = sha256_file(path)
            except OSError as exc:
                raise ValueError("selected reviewer artifact is missing") from exc
            if digest != record.get(f"{kind}_sha256"):
                raise ValueError("selected reviewer artifact hash changed")
        documents[worker_id] = read_result(record["result_path"], manifest)
    return documents


def write_synthesis_inputs(manifest, records):
    _validated_records(manifest, records)
    document = {key: manifest[key] for key in ("round_id", "snapshot_id", "head_sha")}
    document.update(schema_version=SCHEMA_VERSION, records=records)
    path = Path(manifest["artifact_root"]) / "synthesis" / "inputs.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    safe_path(path, manifest["artifact_root"])
    if path.exists() and _read_json(path) != document:
        # A repaired reviewer has a new attempt/hash. Retain the old synthesis
        # input set for audit and seal a distinct set for the next synthesis.
        revision = hashlib.sha256(json.dumps(document, sort_keys=True).encode("utf-8")).hexdigest()
        path = path.with_name(f"inputs-{revision}.json")
        safe_path(path, manifest["artifact_root"])
    if path.exists():
        if _read_json(path) != document:
            raise ValueError("frozen synthesis inputs cannot be replaced")
    else:
        atomic_json(path, document)
    return {"path": str(path), "sha256": sha256_file(path), "records": records}


def read_evaluation(path, manifest, records):
    documents = _validated_records(manifest, records)
    path = safe_path(path, Path(manifest["artifact_root"]) / "synthesis")
    if path.name != "evaluation.json":
        raise ValueError("only the committed evaluation.json is accepted")
    document = _read_json(path)
    _identity(document, manifest)
    expected = {
        (worker, finding["id"])
        for worker, result in documents.items()
        for finding in result["findings"] + result["dismissed_findings"]
    }
    covered, item_ids = set(), set()
    if not isinstance(document.get("items"), list):
        raise ValueError("evaluation.items must be an array")
    for item in document["items"]:
        _finding(item)
        if item["id"] in item_ids:
            raise ValueError("evaluation item ids must be unique")
        item_ids.add(item["id"])
        if item.get("disposition") not in DISPOSITIONS:
            raise ValueError("invalid finding disposition")
        _text(item.get("reason"), "evaluation reason")
        if not isinstance(item.get("sources"), list) or not item["sources"]:
            raise ValueError("every evaluation item requires finding sources")
        for source in item["sources"]:
            if not isinstance(source, dict):
                raise ValueError("finding source must be an object")
            pair = (source.get("worker_id"), source.get("finding_id"))
            if pair not in expected or pair in covered:
                raise ValueError("evaluation contains unknown or duplicated finding source")
            covered.add(pair)
    if covered != expected:
        raise ValueError("evaluation must handle every reviewer finding exactly once")
    if document.get("merge_recommendation") not in RECOMMENDATIONS:
        raise ValueError("invalid merge recommendation")
    _text(document.get("merge_reason"), "merge_reason")
    return document


def _table(value):
    return str(value).replace("|", "\\|").replace("\n", " ")


def render_merged(manifest, records, evaluation):
    result_lookup = _validated_records(manifest, records)
    labels = [f"{record['worker_id']} ({record['model']})" for record in records]
    models = " + ".join(labels)
    distinct = len({record["model"] for record in records}) == len(records)
    mode = "多模型" if distinct and all(record["model"] != "default" for record in records) else "多 reviewer"
    lines = [
        f"# PR #{manifest['pr_number']} Code Review 汇总报告",
        "",
        f"> 由 {models} {mode} review，主 agent 汇总整理。",
        "",
        "## Report",
        "",
        f"审查轮次：{manifest.get('round_no', manifest['round_id'])}；代码快照：{manifest['head_sha']}；"
        f"目标分支：{manifest['base_ref']}；触发：{manifest.get('trigger_url') or manifest.get('trigger_event_fingerprint', manifest['round_id'])}。",
        "",
    ]
    for severity in SEVERITIES:
        lines.extend([f"### {severity.title()} Issues", ""])
        findings = [
            item for item in evaluation["items"] if item["disposition"] == "confirmed" and item["severity"] == severity
        ]
        for finding in findings:
            lines.extend(_finding_lines(finding))
            lines.append(f"  - 复核：{finding['reason']}")
        if not findings:
            lines.append("无")
        lines.append("")
    lines.extend(["### Dismissed Findings", ""])
    dismissed = [item for item in evaluation["items"] if item["disposition"] != "confirmed"]
    for finding in dismissed:
        label = "误报" if finding["disposition"] == "dismissed" else "需人工确认"
        lines.append(f"- **{label}：{finding['title']}** — {_code_span(_location(finding))}：{finding['reason']}")
    if not dismissed:
        lines.append("无")
    lines.extend(
        [
            "",
            "### Review Coverage and Limitations",
            "",
            "以下为各 reviewer 声明的审查范围；未发现缺陷不代表验证全部行为，模型一致也不替代验证证据。",
            "",
        ]
    )
    for record in records:
        coverage = result_lookup[record["worker_id"]]["coverage"]
        reviewed = ", ".join(_code_span(path) for path in coverage["reviewed_paths"]) or "未列出"
        unreviewed = ", ".join(_code_span(path) for path in coverage["unreviewed_paths"]) or "未列出（不表示完整覆盖）"
        lines.extend(
            [
                f"#### {record['worker_id']} ({record['model']})",
                "",
                f"- 已审查：{reviewed}",
                f"- 未审查：{unreviewed}",
                f"- 检查、局限与待确认事项：{coverage['notes']}",
                "",
            ]
        )
    lines.extend(
        [
            "### Agent Agreement Summary",
            "",
            "| Issue | " + " | ".join(_table(label) for label in labels) + " | Verdict |",
            "|-------|" + "|".join("----------" for _ in records) + "|---------|",
        ]
    )
    # Only explicit reviewer dismissals count as a refutation; silence is unreported.
    for item in evaluation["items"]:
        states = []
        for record in records:
            referenced = {
                source["finding_id"] for source in item["sources"] if source["worker_id"] == record["worker_id"]
            }
            result = result_lookup[record["worker_id"]]
            states.append(
                "反驳"
                if any(f["id"] in referenced for f in result["dismissed_findings"])
                else "发现"
                if referenced
                else "未报告"
            )
        verdict = (
            item["severity"]
            if item["disposition"] == "confirmed"
            else ("误报" if item["disposition"] == "dismissed" else "需人工确认")
        )
        lines.append("| " + " | ".join([_table(item["title"]), *states, verdict]) + " |")
    lines.extend(
        [
            "",
            f"**合并建议**：{evaluation['merge_recommendation']}。{evaluation['merge_reason']}",
            "",
            "---",
            f"*由 {models} review，主 agent 汇总 | [from Robert]*",
            "",
        ]
    )
    return "\n".join(lines)
