"""Independent, model-free checkpoints for deriving a public review report.

Only the sealed public copy is returned for report registration. Original inputs,
the rendered private body and redaction evidence remain separate provenance.
The caller retains ownership, database transactions and publication policy.
"""

import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile

from robert_agent.workflows.review import files, workspace


class RedactionBlocked(ValueError):
    def __init__(self, result):
        self.result = result
        super().__init__("review report did not pass public content redaction")


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def code_version(*paths):
    """Pin the actual renderer/redactor module and resource contents."""
    return _hash(_json([{"name": Path(path).name, "sha256": files.sha256_file(path)} for path in (*paths, __file__)]))


def _read(path, root):
    path = files.safe_path(path, root)
    root = Path(root).resolve()
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.relative_to(root).parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("report checkpoint requires regular files")
            raw = stream.read(16 * 1024 * 1024 + 1)
    finally:
        os.close(directory)
    if len(raw) > 16 * 1024 * 1024:
        raise ValueError("report checkpoint input exceeds 16 MiB")
    return raw


def _write(path, raw, root):
    path = files.safe_path(path, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if _read(path, root) != raw:
            raise ValueError("immutable report checkpoint file changed")
        return
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _save(path, payload, root):
    _write(path, _json({"payload": payload, "sha256": _hash(_json(payload))}), root)


def _load(path, root):
    try:
        raw = _read(path, root)
        envelope = json.loads(raw)
        payload = envelope["payload"]
        if (
            not isinstance(payload, dict)
            or raw != _json(envelope)
            or envelope
            != {
                "payload": payload,
                "sha256": _hash(_json(payload)),
            }
        ):
            raise ValueError("report checkpoint hash changed")
        return payload
    except (KeyError, TypeError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid report checkpoint") from exc


def _sources(value):
    """Collect exact controller-provided file identities without interpreting text."""
    result = {}

    def add(path, expected):
        if not isinstance(path, str) or not isinstance(expected, str) or len(expected) != 64:
            raise ValueError("invalid report source identity")
        if path in result and result[path] != expected:
            raise ValueError("conflicting report source identities")
        result[path] = expected

    if isinstance(value, dict):
        for key, item in value.items():
            if key.endswith("_path") and key[:-5] + "_sha256" in value:
                add(item, value[key[:-5] + "_sha256"])
            elif key == "path" and "sha256" in value:
                add(item, value["sha256"])
            elif key == "files" and isinstance(item, dict):
                for path, expected in item.items():
                    add(path, expected)
            for path, expected in _sources(item).items():
                add(path, expected)
    elif isinstance(value, list):
        for item in value:
            for path, expected in _sources(item).items():
                add(path, expected)
    return result


def _source_roots(manifest, evaluation_identity, root):
    """Allow only exact pinned round ancestors, never an expanded artifact scope."""
    hashes = _sources({"manifest": manifest, "evaluation_identity": evaluation_identity})
    allowed = {}
    round_root = root.parent
    candidate = manifest.get("candidate", {})
    original = manifest.get("original_request", {})
    external = _sources({"candidate": candidate, "original_request": original})
    for path in hashes:
        source = Path(path)
        if source.is_relative_to(root):
            allowed[path] = root
        elif (
            root.name == manifest.get("snapshot_id")
            and round_root.name == manifest["round_id"]
            and path in external
            and source.is_relative_to(round_root)
        ):
            # candidate and original_request are controller-pinned provenance
            # carried from implementation into a child snapshot. The exact file
            # and hash are allowed, not arbitrary siblings under the round.
            files.safe_path(source, round_root)
            allowed[path] = round_root
        else:
            raise ValueError("report source is outside its pinned provenance")
    source_hashes = manifest.get("source_hashes", {})
    if source_hashes:
        tree = Path(manifest["worktree_path"]).resolve()
        for name, expected in source_hashes.items():
            source = tree / name
            # Gitlinks and tracked repository symlinks are identity facts, not
            # regular source bytes to dereference. The existing snapshot policy
            # verifies them together with HEAD and the complete source inventory.
            if expected.startswith("gitlink:") or source.is_symlink():
                continue
            path = files.safe_path(tree / name, tree)
            if str(path) in hashes and hashes[str(path)] != expected:
                raise ValueError("conflicting report source identities")
            hashes[str(path)] = expected
            allowed[str(path)] = tree
    return hashes, allowed


def _verify_blob(base, blob, root):
    if not isinstance(blob, dict) or set(blob) != {"name", "sha256"}:
        raise ValueError("invalid report checkpoint file identity")
    path = files.safe_path(base / blob["name"], root)
    if path.parent != base or _hash(_read(path, root)) != blob["sha256"]:
        raise ValueError("report checkpoint file hash changed")
    return path


def _blob(base, name, raw, root):
    path = base / name
    _write(path, raw, root)
    return {"name": name, "sha256": _hash(raw)}


def report_input_identity(
    db_path,
    round_id,
    manifest,
    path,
    sha256,
    *,
    check_active=lambda: None,
    load_anchor=None,
    redact=None,
):
    """Verify a checkpointed report and recover its exact input provenance.

    Return None only for a legacy path outside report-checkpoints. Its workflow
    must validate that exact report against its existing durable registration.
    Missing or corrupt checkpoints at a new report path never become legacy.
    """
    root = Path(manifest["artifact_root"]).resolve()
    path = files.safe_path(path, root)
    if not path.is_relative_to(root / "report-checkpoints"):
        return None
    base = path.parent
    if not all(
        files.safe_path(base / name, root).is_file()
        for name in (
            "private/input.json",
            "render.json",
            "redaction.json",
            "seal.json",
            "public.md",
        )
    ):
        raise ValueError("registered report checkpoint is incomplete")
    saved = _load(base / "private" / "input.json", root)
    identity = saved.get("identity", {})
    if path.name != "public.md" or _hash(_json(identity)) != base.name:
        raise ValueError("report checkpoint input path changed")
    # The exact public hash is already held by the caller's durable session.
    # An old filesystem checkpoint without a seal anchor may be read as legacy
    # provenance only after checking that public hash and all original hashes.
    anchor = load_anchor(base.name) if load_anchor is not None else None
    if anchor is None:
        if _hash(_read(path, root)) != sha256:
            raise ValueError("registered report checkpoint identity changed")
        if identity.get("manifest") != manifest or identity.get("db_path") != str(Path(db_path).resolve()):
            raise ValueError("registered report checkpoint input changed")
        hashes, roots = _source_roots(manifest, identity["evaluation_identity"], root)
        if set(saved.get("originals", {})) != set(hashes):
            raise ValueError("registered report checkpoint originals changed")
        check_active()
        if manifest.get("source_hashes"):
            workspace.verify_snapshot(manifest)
        for source, expected in hashes.items():
            if _hash(_read(source, roots[source])) != expected:
                raise ValueError("registered report checkpoint source changed")
            copy = _verify_blob(base / "private", saved["originals"][source], root)
            if _read(copy, root) != _read(source, roots[source]):
                raise ValueError("registered report checkpoint original changed")
        return identity["evaluation_identity"]

    def unavailable(*_args):
        raise ValueError("registered report checkpoint is incomplete")

    verified = seal_rendered_report(
        db_path,
        round_id,
        manifest,
        identity["evaluation_identity"],
        unavailable,
        redact=redact or unavailable,
        renderer_version=identity["renderer_version"],
        redaction_version=identity["redaction_version"],
        check_active=check_active,
        load_anchor=load_anchor,
    )
    if verified["path"] != path or verified["sha256"] != sha256:
        raise ValueError("registered report checkpoint identity changed")
    return identity["evaluation_identity"]


def seal_rendered_report(
    db_path,
    round_id,
    manifest,
    evaluation_identity,
    render,
    *,
    redact,
    renderer_version,
    redaction_version,
    check_active=lambda: None,
    register=None,
    load_anchor=None,
    save_anchor=None,
):
    """Resume render → redaction → seal → idempotent caller registration.

    ``evaluation_identity`` contains records and, for merged reports, the exact
    registered evaluation_path/evaluation_sha256. The caller passes its existing
    redactor unchanged. A blocked result keeps private evidence and retries only
    redaction; changed code contents select a new input identity, never a model.
    ``register(descriptor)`` runs after sealing, including retries after a failed
    registration. It must enforce the caller's current ownership and transaction.
    ``load_anchor(input_sha256)`` returns a trusted {path, sha256} seal receipt;
    ``save_anchor(descriptor)`` persists it in a separate transaction before
    report registration. Cached files without a trusted anchor are rederived
    from verified sources and compared exactly before receiving any trust.
    """
    if not renderer_version or not redaction_version or manifest.get("round_id") != round_id:
        raise ValueError("report checkpoint version or round identity missing")
    # Snapshot values so callbacks cannot mutate the identity we are committing.
    current_manifest, current_evaluation = manifest, evaluation_identity
    identity = json.loads(
        _json(
            {
                "schema_version": 1,
                "db_path": str(Path(db_path).resolve()),
                "round_id": round_id,
                "manifest": manifest,
                "evaluation_identity": evaluation_identity,
                "renderer_version": renderer_version,
                "redaction_version": redaction_version,
            }
        )
    )
    manifest = identity["manifest"]
    evaluation_identity = identity["evaluation_identity"]
    records = evaluation_identity.get("records")
    legacy = evaluation_identity.get("legacy_report")
    if (
        not isinstance(records, list)
        or not records
        and not (
            isinstance(legacy, dict)
            and legacy.get("path")
            and legacy.get("sha256")
            and legacy.get("round_id") == round_id
        )
    ):
        raise ValueError("report checkpoint requires exact reviewer records")
    for record in records:
        for kind in ("result", "report"):
            if not record.get(kind + "_path") or not record.get(kind + "_sha256"):
                raise ValueError("report checkpoint reviewer identity missing")
    if len(records) > 1 or "evaluation_path" in evaluation_identity:
        if not evaluation_identity.get("evaluation_path") or not evaluation_identity.get("evaluation_sha256"):
            raise ValueError("report checkpoint requires exact evaluation identity")

    root = Path(manifest["artifact_root"]).resolve()
    input_sha = _hash(_json(identity))
    base = files.safe_path(root / "report-checkpoints" / input_sha, root)
    cached_render = (base / "render.json").exists()
    anchor = load_anchor(input_sha) if load_anchor is not None else None

    def verify_anchor(value):
        if (
            not isinstance(value, dict)
            or set(value) != {"path", "sha256"}
            or value["path"] != str(base / "seal.json")
            or value["sha256"] != _hash(_read(base / "seal.json", root))
        ):
            raise ValueError("trusted report seal anchor changed")

    if anchor is not None:
        verify_anchor(anchor)
    private = files.safe_path(base / "private", root)
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    private.mkdir(exist_ok=True, mode=0o700)
    os.chmod(base, 0o700)
    os.chmod(private, 0o700)
    source_hashes, source_roots = _source_roots(manifest, evaluation_identity, root)

    def read_source(path):
        return _read(path, source_roots[path])

    def verify_inputs():
        check_active()
        if manifest.get("source_hashes"):
            workspace.verify_snapshot(manifest)
        for path, expected in source_hashes.items():
            if _hash(read_source(path)) != expected:
                raise ValueError("report checkpoint source input hash changed")
        for record in records:
            files.read_result(record["result_path"], manifest)
        if evaluation_identity.get("evaluation_path"):
            files.read_evaluation(evaluation_identity["evaluation_path"], manifest, records)
        if _json(identity) != _json(
            {
                "schema_version": 1,
                "db_path": str(Path(db_path).resolve()),
                "round_id": round_id,
                "manifest": current_manifest,
                "evaluation_identity": current_evaluation,
                "renderer_version": renderer_version,
                "redaction_version": redaction_version,
            }
        ):
            raise ValueError("report checkpoint input identity changed")

    verify_inputs()
    input_path = private / "input.json"
    if not input_path.exists():
        if any((base / name).exists() for name in ("render.json", "redaction.json", "seal.json")):
            raise ValueError("report checkpoint input receipt missing")
        originals = {}
        for path, expected in source_hashes.items():
            raw = read_source(path)
            if _hash(raw) != expected:
                raise ValueError("report checkpoint source input hash changed")
            originals[path] = _blob(private, "original-" + expected, raw, root)
        _save(input_path, {"identity": identity, "originals": originals}, root)
    saved_input = _load(input_path, root)
    if saved_input.get("identity") != identity or set(saved_input.get("originals", {})) != set(source_hashes):
        raise ValueError("report checkpoint input identity changed")
    for path, blob in saved_input["originals"].items():
        if blob.get("sha256") != source_hashes[path]:
            raise ValueError("report checkpoint original hash changed")
        original_path = _verify_blob(private, blob, root)
        if _read(original_path, root) != read_source(path):
            raise ValueError("report checkpoint original bytes changed")

    render_path = base / "render.json"
    if not render_path.exists():
        if (base / "redaction.json").exists() or (base / "seal.json").exists():
            raise ValueError("report render receipt missing")
        body = render()
        if not isinstance(body, str) or not body.strip():
            raise ValueError("rendered review report must be nonempty text")
        verify_inputs()
        blob = _blob(private, "rendered.md", body.encode("utf-8"), root)
        _save(render_path, {"input_sha256": input_sha, "file": blob}, root)
    rendered = _load(render_path, root)
    if set(rendered) != {"input_sha256", "file"} or rendered["input_sha256"] != input_sha:
        raise ValueError("report render checkpoint changed")
    raw_path = _verify_blob(private, rendered["file"], root)
    render_sha = _hash(_read(render_path, root))
    if anchor is None and cached_render:
        # A worker can forge every self-reported SHA. Recompute deterministic
        # render semantics from controller-validated source inputs before trusting
        # a filesystem-only result, including a crash before the first DB anchor.
        fresh = render()
        verify_inputs()
        if not isinstance(fresh, str) or fresh.encode("utf-8") != _read(raw_path, root):
            raise ValueError("unanchored cached report render derivation changed")

    redaction_path = base / "redaction.json"
    cached_redaction = redaction_path.exists()
    if not redaction_path.exists():
        if (base / "seal.json").exists():
            raise ValueError("report redaction receipt missing")
        result = redact(_read(raw_path, root).decode("utf-8"))
        verify_inputs()
        _verify_blob(private, rendered["file"], root)
        if result.get("ok") is not True:
            # This is evidence, not a successful redaction receipt. Explicit
            # retries rerun the same policy against the retained private body.
            evidence = {"input_sha256": input_sha, "render_sha256": render_sha, "result": result}
            _save(private / ("blocked-" + _hash(_json(evidence)) + ".json"), evidence, root)
            raise RedactionBlocked(result)
        if not isinstance(result.get("text"), str) or not result["text"].strip():
            raise ValueError("redacted review report must be nonempty text")
        blob = _blob(private, "redacted.md", result["text"].encode("utf-8"), root)
        evidence = {key: value for key, value in result.items() if key != "text"}
        _save(
            redaction_path,
            {
                "input_sha256": input_sha,
                "render_sha256": render_sha,
                "file": blob,
                "result": evidence,
            },
            root,
        )
    redacted = _load(redaction_path, root)
    if (
        set(redacted) != {"input_sha256", "render_sha256", "file", "result"}
        or redacted["input_sha256"] != input_sha
        or redacted["render_sha256"] != render_sha
        or redacted["result"].get("ok") is not True
    ):
        raise ValueError("report redaction checkpoint changed")
    redacted_path = _verify_blob(private, redacted["file"], root)
    if anchor is None and cached_redaction:
        checked = redact(_read(raw_path, root).decode("utf-8"))
        verify_inputs()
        if checked.get("ok") is not True:
            raise RedactionBlocked(checked)
        if not isinstance(checked.get("text"), str) or checked["text"].encode("utf-8") != _read(redacted_path, root):
            raise ValueError("unanchored cached report public derivation changed")
    redaction_sha = _hash(_read(redaction_path, root))

    seal_path = base / "seal.json"
    seal = {
        "input_sha256": input_sha,
        "input_checkpoint_sha256": _hash(_read(input_path, root)),
        "render_sha256": render_sha,
        "redaction_sha256": redaction_sha,
        "public": {"name": "public.md", "sha256": redacted["file"]["sha256"]},
    }
    if not seal_path.exists():
        verify_inputs()
        _write(base / "public.md", _read(redacted_path, root), root)
        _save(seal_path, seal, root)
    if _load(seal_path, root) != seal:
        raise ValueError("report seal checkpoint changed")
    public_path = _verify_blob(base, seal["public"], root)
    verify_inputs()
    descriptor = {
        "path": public_path,
        "sha256": seal["public"]["sha256"],
        "input_sha256": input_sha,
        "checkpoint_path": seal_path,
        "checkpoint_sha256": _hash(_read(seal_path, root)),
        "evaluation_identity": evaluation_identity,
    }
    if save_anchor is not None:
        if load_anchor is None:
            raise ValueError("report seal anchor requires a trusted loader")
        if anchor is None:
            saved = save_anchor(descriptor)
            if saved is False or isinstance(saved, dict) and saved.get("ok") is False:
                raise ValueError("report seal anchor registration refused")
            anchor = load_anchor(input_sha)
        verify_anchor(anchor)
    elif anchor is not None:
        verify_anchor(anchor)
    if register is not None:
        registered = register(descriptor)
        if registered is False or isinstance(registered, dict) and registered.get("ok") is False:
            raise ValueError("report registration refused")
    return descriptor
