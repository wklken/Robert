from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SEARCH_ROOTS = [ROOT / "src", ROOT / "tests"]
FORBIDDEN = [
    "dd-" + "github-agent",
    "dd-" + "github-worker",
    "dd_" + "agent",
    "dd_" + "account",
    "dd-" + "comment",
    "dd-" + "workstream",
    "X-" + "DD-CSRF-Token",
    "Tencent" + "BlueKing",
    "robot-" + "wklken",
    "/root/" + ".agents",
    "~/" + ".agents",
]
ALLOWED_LEGACY_PATHS = {
    FORBIDDEN[0]: {
        "src/robert_agent/cli/main.py",
        "src/robert_agent/migrate.py",
        "tests/test_migration.py",
    },
    FORBIDDEN[3]: {
        "src/robert_agent/migrate.py",
        "tests/test_migration.py",
    },
    FORBIDDEN[4]: {
        "src/robert_agent/audit_result.py",
        "src/robert_agent/publish.py",
        "tests/test_migration.py",
    },
    FORBIDDEN[5]: {
        "src/robert_agent/audit_result.py",
        "src/robert_agent/publish.py",
        "tests/test_migration.py",
    },
    FORBIDDEN[10]: {
        "src/robert_agent/cli/main.py",
        "tests/test_migration.py",
    },
}


class PublicSurfaceTests(unittest.TestCase):
    def test_public_tree_has_no_private_or_legacy_identifiers(self):
        failures = []
        for root in SEARCH_ROOTS:
            for path in root.rglob("*"):
                if not path.is_file() or path.suffix in {".pyc", ".sqlite3"}:
                    continue
                text = path.read_text(encoding="utf-8", errors="ignore")
                for token in FORBIDDEN:
                    relative = str(path.relative_to(ROOT))
                    if (
                        token in text
                        and relative not in ALLOWED_LEGACY_PATHS.get(token, set())
                    ):
                        failures.append(f"{relative}: {token}")
        self.assertEqual(failures, [])


if __name__ == "__main__":
    unittest.main()
