"""Record full-suite evidence and serialize runners using the same test tree."""

from contextlib import contextmanager
import errno
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import sys
import tempfile


REPO_ROOT = Path(__file__).resolve().parents[2]
PYCACHE_ROOT = Path.home() / ".cache" / "robert-test-pycache"


class RunAlreadyActiveError(RuntimeError):
    pass


def source_digest(start_directory):
    """Include current source bytes, including uncommitted and newly added tests."""
    roots = (REPO_ROOT / "src", REPO_ROOT / "tests")
    paths = {
        path
        for root in roots
        for path in root.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix not in {".pyc", ".pyo", ".log"}
    }
    # Custom runner-contract suites create barriers and reports beside their code.
    # These execution artifacts are not additional test or package inputs.
    paths.update(start_directory.resolve().rglob("*.py"))
    paths.add(REPO_ROOT / "pyproject.toml")
    result = hashlib.sha256()
    for path in sorted(paths):
        name = os.fsencode(path)
        contents = path.read_bytes()
        result.update(len(name).to_bytes(8, "big") + name)
        result.update(len(contents).to_bytes(8, "big") + contents)
    return result.hexdigest()


def environment_record():
    return {
        "executable": str(Path(sys.executable).resolve()),
        "python": sys.version,
        "packages": dict(sorted((dist.metadata["Name"], dist.version) for dist in metadata.distributions())),
        "source_root": str(REPO_ROOT / "src"),
        "temporary_root": tempfile.gettempdir(),
    }


@contextmanager
def run_lock(start_directory):
    """Keep the lock inode stable and let the OS release ownership on exit."""
    key = hashlib.sha256(os.fsencode(start_directory.resolve())).hexdigest()
    # A task's TMPDIR can change between calls; keep duplicate detection outside it.
    directory = Path.home() / ".cache" / "robert-test-runs"
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / (key + ".lock")).open("a+") as stream:
        try:
            if os.name == "nt":
                import msvcrt

                stream.seek(0)
                if not stream.read(1):
                    stream.write(" ")
                    stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno not in {errno.EACCES, errno.EAGAIN}:
                raise
            raise RunAlreadyActiveError(
                f"Tests are already running for {start_directory.resolve()}; wait for that run"
            ) from error
        yield


def write_report(path, value):
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, indent=2)
            stream.write("\n")
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
