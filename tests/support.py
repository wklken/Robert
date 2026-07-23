import os
from pathlib import Path
import sys


PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "src" / "robert_agent"
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
