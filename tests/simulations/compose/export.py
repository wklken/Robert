"""Stream bounded synthetic evidence without copying the live database or socket."""

from pathlib import Path
import sys
import tarfile


ROOT = Path("/state")
LIMIT = 48 * 1024 * 1024


def main():
    total = 0
    with tarfile.open(fileobj=sys.stdout.buffer, mode="w|gz") as archive:
        for name in (
            "result.json",
            "timeline.jsonl",
            "installation.json",
            "config.json",
            "logs",
            "evidence",
            "actors",
            "data/artifacts",
        ):
            path = ROOT / name
            paths = sorted(path.rglob("*")) if path.is_dir() else (path,)
            for candidate in paths:
                if not candidate.is_file() or candidate.is_symlink():
                    continue
                total += candidate.stat().st_size
                if total > LIMIT:
                    raise ValueError("scenario artifacts exceeded the size limit")
                archive.add(candidate, arcname=str(candidate.relative_to(ROOT)), recursive=False)


if __name__ == "__main__":
    main()
