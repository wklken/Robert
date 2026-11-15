"""Bounded, descriptor-based text evidence from explicitly mapped repositories."""

from dataclasses import dataclass, field
from hashlib import sha256
import os
from pathlib import Path
import stat
from types import MappingProxyType

from robert_agent.security.redaction import redact_text

MAX_FILE_BYTES = 8192
MAX_TOTAL_BYTES = 24000
MAX_FILES = 24
MAX_RESULTS = 12
_INITIAL_PATHS = ("README.md", "README", "pyproject.toml", "go.mod", "package.json", "Cargo.toml", "pom.xml")
_DENIED_COMPONENTS = frozenset({".git", ".ssh", ".aws", ".env", ".npmrc", ".netrc"})


class EvidenceError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class FileEvidence:
    repo_id: str
    path: str
    revision: str
    text: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class EvidenceOmission:
    repo_id: str
    path: str
    reason: str


@dataclass(frozen=True, slots=True)
class EvidenceBatch:
    records: tuple[FileEvidence, ...]
    omissions: tuple[EvidenceOmission, ...]


def _parts(path):
    if not isinstance(path, str) or not 1 <= len(path) <= 512 or any(ord(char) < 32 for char in path) or "\\" in path:
        raise EvidenceError("invalid_path")
    parts = path.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise EvidenceError("invalid_path")
    if any(
        part.casefold() in _DENIED_COMPONENTS
        or part.casefold().startswith(".env.")
        or part.casefold().endswith((".pem", ".key", ".p12"))
        for part in parts
    ):
        raise EvidenceError("sensitive_path")
    return parts


def _snapshot(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


class EvidenceReader:
    """Use one reader per evidence collection to enforce aggregate read limits."""

    def __init__(self, repositories):
        self._roots = MappingProxyType({key: Path(path) for key, path in repositories.items()})
        if any(not isinstance(key, str) or not key or not value.is_absolute() for key, value in self._roots.items()):
            raise EvidenceError("invalid_repository_mapping")
        if os.open not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW"):
            raise EvidenceError("unsupported_filesystem")
        self._files = 0
        self._bytes = 0
        self._opened = {}

    def __enter__(self):
        if self._opened:
            raise EvidenceError("reader_already_open")
        return self

    def __exit__(self, *exc):
        for descriptor in self._opened.values():
            os.close(descriptor)
        self._opened.clear()

    def _root(self, repo_id):
        if repo_id not in self._roots:
            raise EvidenceError("repository_out_of_scope")
        if repo_id not in self._opened:
            # Configured root is the trusted filesystem boundary. Its final
            # component must be an actual directory, never a supplied symlink.
            self._opened[repo_id] = os.open(self._roots[repo_id], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        return self._opened[repo_id]

    def read(self, repo_id, path):
        parts = _parts(path)
        if self._files >= MAX_FILES or self._bytes >= MAX_TOTAL_BYTES:
            raise EvidenceError("evidence_budget")
        self._files += 1
        try:
            descriptor = os.dup(self._root(repo_id))
            try:
                for part in parts[:-1]:
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                    os.close(descriptor)
                    descriptor = child
                child = os.open(parts[-1], os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=descriptor)
                try:
                    before = os.fstat(child)
                    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                        raise EvidenceError("nonregular_file")
                    if before.st_size > MAX_FILE_BYTES:
                        raise EvidenceError("file_limit")
                    limit = min(MAX_FILE_BYTES, MAX_TOTAL_BYTES - self._bytes)
                    data = bytearray()
                    while len(data) <= limit:
                        chunk = os.read(child, min(4096, limit + 1 - len(data)))
                        if not chunk:
                            break
                        data.extend(chunk)
                    self._bytes += len(data)
                    if len(data) > limit:
                        raise EvidenceError("file_limit" if limit == MAX_FILE_BYTES else "evidence_budget")
                    if len(data) != before.st_size or _snapshot(before) != _snapshot(os.fstat(child)):
                        raise EvidenceError("file_changed")
                finally:
                    os.close(child)
            finally:
                os.close(descriptor)
        except FileNotFoundError:
            raise EvidenceError("file_missing") from None
        except OSError:
            raise EvidenceError("path_unavailable") from None
        try:
            text = data.decode("utf-8")
        except UnicodeError:
            raise EvidenceError("non_text_file") from None
        if "\0" in text:
            raise EvidenceError("non_text_file")
        if redact_text(text, strict_assignments=True).get("text") != text:
            raise EvidenceError("sensitive_text")
        return FileEvidence(repo_id, path, "sha256:" + sha256(data).hexdigest(), text)

    def _collect(self, candidates, accept):
        records, omissions = [], []
        for repo_id, path in candidates:
            if len(records) == MAX_RESULTS or len(records) + len(omissions) == MAX_FILES:
                omissions.append(EvidenceOmission(repo_id, path, "collection_limit"))
                break
            try:
                proof = self.read(repo_id, path)
            except EvidenceError as error:
                omissions.append(EvidenceOmission(repo_id, path, error.code))
                if error.code == "evidence_budget":
                    break
            else:
                if accept(proof):
                    records.append(proof)
        return EvidenceBatch(tuple(records), tuple(omissions))

    def initial(self):
        return self._collect(
            ((repo_id, path) for path in _INITIAL_PATHS for repo_id in sorted(self._roots)),
            lambda _: True,
        )

    def search(self, repo_id, query, paths):
        if not isinstance(query, str) or not 1 <= len(query) <= 128 or not query.strip():
            raise EvidenceError("invalid_search")
        if repo_id not in self._roots:
            raise EvidenceError("repository_out_of_scope")
        if isinstance(paths, (str, bytes)):
            raise EvidenceError("invalid_search_paths")
        term = query.casefold()
        return self._collect(((repo_id, path) for path in paths), lambda proof: term in proof.text.casefold())
