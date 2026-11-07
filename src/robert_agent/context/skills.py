from pathlib import Path
import re


NAME_RE = re.compile(r"^name:\s*(?P<name>.+?)\s*$", re.MULTILINE)


def discover_skill_names(search_paths):
    names = set()
    for raw_path in search_paths:
        root = Path(raw_path).expanduser()
        if not root.exists():
            continue
        for manifest in root.rglob("SKILL.md"):
            match = NAME_RE.search(manifest.read_text(encoding="utf-8"))
            if match:
                names.add(match.group("name").strip())
    return names


def route_skill_status(required, recommended, installed):
    missing_required = sorted(set(required) - installed)
    missing_recommended = sorted(set(recommended) - installed)
    return {
        "runnable": not missing_required,
        "missing_required": missing_required,
        "missing_recommended": missing_recommended,
    }
