import os
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[2]
TEST_ROOT = REPO_ROOT / "tests"
PACKAGE_ROOT = REPO_ROOT / "src" / "robert_agent"
PACKAGE_PARENT = str(PACKAGE_ROOT.parent)
if PACKAGE_PARENT not in sys.path:
    sys.path.insert(0, PACKAGE_PARENT)

existing_pythonpath = os.environ.get("PYTHONPATH")
os.environ["PYTHONPATH"] = (
    PACKAGE_PARENT
    if not existing_pythonpath
    else os.pathsep.join([PACKAGE_PARENT, existing_pythonpath])
)


def resource_text(*parts: str) -> str:
    from robert_agent.resource_files import resource

    return resource(*parts).read_text(encoding="utf-8")


DAEMON_DEFAULTS = {
    "enabled": True,
    "local_poll_seconds": 5,
    "github_poll_seconds": 300,
    "github_poll_when_full_seconds": 600,
    "rate_limit_cache_seconds": 300,
    "min_search_remaining": 10,
    "min_core_remaining": 500,
    "live_run_timeout_seconds": 300,
    "local_drain_timeout_seconds": 180,
    "run_on_start": False,
}


def maintenance_config():
    return {
        "enabled": True,
        "interval_seconds": 300,
        "batch_size": 100,
        "retention": {"runs_days": 7, "failed_runs_days": 30, "daemon_events_days": 7},
    }


def init_test_checkout(path, branch="main"):
    """A local-only upstream for tests exercising the public workspace policy."""
    import subprocess

    for args in (
        ["git", "init", "-b", branch],
        [
            "git",
            "-c",
            "user.name=Config test",
            "-c",
            "user.email=config@example.invalid",
            "commit",
            "--allow-empty",
            "-m",
            "Initial test checkout",
        ],
        ["git", "remote", "add", "upstream", str(path)],
    ):
        subprocess.run(args, cwd=path, check=True, capture_output=True)
