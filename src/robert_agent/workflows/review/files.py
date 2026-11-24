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

from robert_agent.foundation.contracts.execution import ReviewReportMode
from robert_agent.foundation.review_findings import deterministic_finding_id, review_requires_followup


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


def safe_path(path, root, *, subdirectory=None):
    """Reject links/escapes inside an already trusted artifact directory."""
    configured_root = Path(os.path.abspath(root))
    root = configured_root.resolve()
    path = Path(os.path.abspath(path))
    try:
        relative = path.relative_to(configured_root if path.is_relative_to(configured_root) else root)
    except ValueError as exc:
        raise ValueError("review artifact is outside its registered directory") from exc
    if subdirectory is not None and not relative.is_relative_to(subdirectory):
        raise ValueError("review artifact is outside its registered directory")
    # Resolve only the trusted root; links below it must remain visible to the checks.
    path = root / relative
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("review artifact path contains a symlink")
    if path.resolve() != path:
        raise ValueError("review artifact path escapes its registered directory")
    return path


def atomic_json(path, payload):
    atomic_text(path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def atomic_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError("cannot replace a symlink artifact")
    # A unique temporary name also makes controller writes safe across retries.
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
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
    path = safe_path(path, manifest["artifact_root"], subdirectory="runs")
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
    if (
        manifest.get("review_report_mode") == ReviewReportMode.DETERMINISTIC_V1
        and type(document.get("requires_judgment")) is not bool
    ):
        raise ValueError("review requires an explicit requires_judgment boolean")
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
            path = safe_path(record.get(f"{kind}_path", ""), manifest["artifact_root"], subdirectory="runs")
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
    path = safe_path(path, manifest["artifact_root"], subdirectory="synthesis")
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
    title = f"PR #{manifest['pr_number']}" if manifest["pr_number"] else "候选提交"
    trigger = manifest.get("trigger_url") or manifest.get("trigger_event_fingerprint", manifest["round_id"])
    context = f"；触发：{trigger}" if manifest["pr_number"] else ""
    lines = [
        f"# {title} Code Review 汇总报告",
        "",
        f"> 由 {models} {mode} review，主 agent 汇总整理。",
        "",
        "## Report",
        "",
        f"审查轮次：{manifest.get('round_no', manifest['round_id'])}；代码快照：{manifest['head_sha']}；"
        f"目标分支：{manifest['base_ref']}{context}。",
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
            "### Agent Agreement Summary",
            "",
            "| Issue | " + " | ".join(_table(record["worker_id"]) for record in records) + " | Verdict |",
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


def merge_reviewer_results(manifest, records):
    """Project every source into neutral, reproducible groups without adjudicating."""
    documents = _validated_records(manifest, records)
    groups = {}
    reviewers = []
    for record in sorted(records, key=lambda record: record["worker_id"]):
        worker = record["worker_id"]
        result = documents[worker]
        reviewers.append(
            {
                "worker_id": worker,
                "model": record["model"],
                "requires_judgment": result["requires_judgment"],
                "merge_recommendation": result["merge_recommendation"],
                "coverage": {key: result["coverage"][key] for key in ("reviewed_paths", "unreviewed_paths", "notes")},
            }
        )
        for kind in ("findings", "dismissed_findings"):
            for finding in sorted(result[kind], key=lambda finding: finding["id"]):
                # Group only the same concrete claim at the same range. Reviewer
                # IDs are local to one run and cannot identify cross-review duplicates.
                key = deterministic_finding_id(finding)
                group = groups.setdefault(key, [])
                group.append({"worker_id": worker, "kind": kind, "finding": finding})
    items = [{"id": key, "sources": sources} for key, sources in sorted(groups.items())]
    requires_followup = any(review_requires_followup(result) for result in documents.values())
    return {"items": items, "reviewers": reviewers, "requires_followup": requires_followup}


def neutral_report_identity(merged):
    return {
        "requires_followup": merged["requires_followup"],
        "finding_groups": [
            {
                "id": item["id"],
                "sources": [
                    {"worker_id": source["worker_id"], "finding_id": source["finding"]["id"]}
                    for source in item["sources"]
                ],
            }
            for item in merged["items"]
        ],
    }


def render_neutral_report(manifest, merged):
    lines = [
        "# 候选提交 Review 汇总报告",
        "",
        f"送审提交：`{manifest['head_sha']}`",
        f"目标分支：{_code_span(manifest['base_ref'])}",
        "结果由程序按来源归并。每个 finding 仍待核查。合并建议仅代表各 reviewer 的判断。",
        "",
        "## Finding 与来源",
        "",
    ]
    for item in merged["items"]:
        sources = item["sources"]
        lines.append(f"### {item['id']}")
        if len({(source["kind"], source["finding"]["severity"]) for source in sources}) > 1:
            lines.append("**分歧待核查。**")
        lines.append("")
        for source in sources:
            finding = source["finding"]
            stance = "反证" if source["kind"] == "dismissed_findings" else "报告问题"
            lines.append(f"- {_code_span(source['worker_id'])} · {_code_span(finding['id'])} · {stance}")
            lines.extend("  " + line for line in _finding_lines(finding))
            location = finding["location"]
            if location.get("end_line", location["line"]) != location["line"]:
                lines.append(f"    - 结束行：{location['end_line']}")
            if source["kind"] == "dismissed_findings":
                lines.append(f"    - 反证理由：{finding['reason']}")
        lines.append("")
    if not merged["items"]:
        lines.extend(["Reviewer 未报告 finding。", ""])
    lines.extend(["## Reviewer 覆盖范围与建议", ""])
    for reviewer in merged["reviewers"]:
        coverage = json.dumps(reviewer["coverage"], ensure_ascii=False)
        lines.extend(
            [
                f"- {_code_span(reviewer['worker_id'])} ({_code_span(reviewer['model'])}) "
                f"· {reviewer['merge_recommendation']}",
                f"  - {'待核查' if reviewer['requires_judgment'] else '未标记额外核查'}",
                f"  - {coverage}",
            ]
        )
    next_step = "核查报告与覆盖限制，按需处置。" if merged["requires_followup"] else "核对送审提交的验证证据。"
    lines.extend(["", f"下一步：{next_step}", ""])
    return "\n".join(lines)
