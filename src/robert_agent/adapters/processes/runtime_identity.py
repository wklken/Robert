"""Bind a trusted build sidecar to source bytes and a running daemon instance."""

from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import tarfile
import zipfile

from robert_agent.adapters.processes import identity as worker_identity


_PACKAGE = "robert_agent"
_SOURCE = "src/" + _PACKAGE
_MANIFEST_FIELDS = {
    "schema_version",
    "source_revision",
    "source_digest",
    "artifact_sha256",
    "build_id",
}
_MAX_PACKAGE = 32 * 1024 * 1024
_MAX_ARCHIVE = 64 * 1024 * 1024


def _git(repo, *args):
    result = subprocess.run(
        ["git", *args], cwd=repo, check=False, capture_output=True, timeout=15
    )
    if result.returncode:
        raise ValueError("could not read the Git build source")
    return result.stdout


def _add(entries, name, content):
    path = PurePosixPath(name)
    if (
        path.is_absolute()
        or ".." in path.parts
        or "\\" in name
        or not path.parts
        or str(path) != name
        or name in entries
        or len(entries) >= 2048
    ):
        raise ValueError("unsafe or duplicate package member")
    entries[name] = sha256(content).hexdigest()


def _source_digest(entries):
    if "__init__.py" not in entries:
        raise ValueError("Robert package entry is missing")
    return sha256(
        json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _git_package_digest(repo, revision):
    archive = _git(repo, "archive", "--format=tar", revision, _SOURCE)
    if len(archive) > _MAX_ARCHIVE:
        raise ValueError("Git build source exceeds the package limit")
    entries = {}
    total = 0
    with tarfile.open(fileobj=BytesIO(archive), mode="r:") as source:
        for member in source:
            if member.isdir():
                continue
            if not member.isfile() or not member.name.startswith(_SOURCE + "/"):
                raise ValueError(
                    "Git build source contains an unsupported package member"
                )
            total += member.size
            if total > _MAX_PACKAGE:
                raise ValueError("Git build source exceeds the package limit")
            with source.extractfile(member) as stream:
                _add(entries, member.name.removeprefix(_SOURCE + "/"), stream.read())
    return _source_digest(entries)


def _wheel_package_digest(contents):
    entries = {}
    total = 0
    with zipfile.ZipFile(BytesIO(contents)) as wheel:
        for member in wheel.infolist():
            if member.is_dir() or not member.filename.startswith(_PACKAGE + "/"):
                continue
            total += member.file_size
            if total > _MAX_PACKAGE or stat.S_ISLNK(member.external_attr >> 16):
                raise ValueError("unsupported or oversized wheel package member")
            _add(
                entries,
                member.filename.removeprefix(_PACKAGE + "/"),
                wheel.read(member),
            )
    return _source_digest(entries)


def _installed_package_digest(root):
    root = Path(root).resolve()
    entries = {}
    total = 0
    for directory, dirs, filenames in os.walk(root, followlinks=False):
        dirs[:] = [name for name in dirs if name != "__pycache__"]
        parent = Path(directory)
        if any((parent / name).is_symlink() for name in dirs):
            raise ValueError("installed package contains a directory symlink")
        for name in filenames:
            path = parent / name
            info = path.lstat()
            total += info.st_size
            if not stat.S_ISREG(info.st_mode) or total > _MAX_PACKAGE:
                raise ValueError("unsupported or oversized installed package member")
            _add(entries, path.relative_to(root).as_posix(), path.read_bytes())
    return _source_digest(entries)


def _build_id(fields):
    return (
        "robert-build-"
        + sha256(
            json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )


def build_manifest(repo_root, wheel_path):
    """Create a sidecar for an actual wheel matching the clean Git source tree."""
    repo = Path(repo_root).resolve()
    revision = _git(repo, "rev-parse", "HEAD").decode("ascii").strip()
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", revision):
        raise ValueError("invalid Git source revision")
    if _git(repo, "status", "--porcelain=v1", "--untracked-files=normal"):
        raise ValueError("build provenance requires a clean Git checkout")
    with Path(wheel_path).open("rb") as stream:
        contents = stream.read(_MAX_ARCHIVE + 1)
    if len(contents) > _MAX_ARCHIVE:
        raise ValueError("wheel exceeds the provenance archive limit")
    try:
        digest = _wheel_package_digest(contents)
        source_digest = _git_package_digest(repo, revision)
    except (zipfile.BadZipFile, tarfile.TarError) as exc:
        raise ValueError("cannot read the wheel or Git package archive") from exc
    if digest != source_digest:
        raise ValueError("wheel package files do not match the committed Git source")
    if _git(repo, "rev-parse", "HEAD").decode("ascii").strip() != revision or _git(
        repo, "status", "--porcelain=v1", "--untracked-files=normal"
    ):
        raise ValueError("Git source changed during provenance capture")
    fields = {
        "schema_version": 1,
        "source_revision": revision,
        "source_digest": digest,
        "artifact_sha256": sha256(contents).hexdigest(),
    }
    return {**fields, "build_id": _build_id(fields)}


def _read_manifest(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_mode & 0o022
            or info.st_uid not in {0, os.geteuid()}
            or info.st_size > 8192
        ):
            raise ValueError("build sidecar must be a trusted regular file")
        raw = stream.read(8193)
    if len(raw) > 8192:
        raise ValueError("build sidecar is too large")
    value = json.loads(raw)
    if (
        not isinstance(value, dict)
        or set(value) != _MANIFEST_FIELDS
        or type(value["schema_version"]) is not int
        or value["schema_version"] != 1
    ):
        raise ValueError("unsupported build sidecar schema")
    for key in ("source_revision", "source_digest", "artifact_sha256"):
        item = value[key]
        pattern = (
            r"[0-9a-f]{40}|[0-9a-f]{64}"
            if key == "source_revision"
            else r"[0-9a-f]{64}"
        )
        if not isinstance(item, str) or not re.fullmatch(pattern, item):
            raise ValueError("invalid build sidecar digest")
    if value["build_id"] != _build_id(
        {key: item for key, item in value.items() if key != "build_id"}
    ):
        raise ValueError("build sidecar ID differs from its contents")
    return value


def runtime_snapshot(*, run_id, started_at, manifest_path=None, package_root=None):
    """Capture once at startup. Only the running installation can attest bytes."""
    snapshot = {
        "run_id": run_id,
        "started_at": started_at,
        "pid": os.getpid(),
        "build_status": "unverified",
    }
    try:
        snapshot["process_identity"] = worker_identity.capture_process_identity(
            os.getpid()
        )
    except (OSError, worker_identity.IdentityReadError):
        return {**snapshot, "reason_code": "runtime_process_unverified"}
    if manifest_path is None:
        return {**snapshot, "reason_code": "build_manifest_not_configured"}
    try:
        manifest = _read_manifest(Path(manifest_path).expanduser())
        actual = _installed_package_digest(
            package_root or Path(__file__).resolve().parents[2]
        )
    except (OSError, ValueError):
        return {**snapshot, "reason_code": "build_manifest_or_package_unreadable"}
    if actual != manifest["source_digest"]:
        return {**snapshot, "reason_code": "running_package_digest_mismatch"}
    return {**snapshot, **manifest, "build_status": "verified"}
