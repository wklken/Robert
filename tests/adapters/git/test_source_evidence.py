from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from robert_agent.adapters.git.source_evidence import EvidenceError, EvidenceReader


class SourceEvidenceTests(unittest.TestCase):
    def test_scope_symlink_and_bounded_evidence(self):
        with TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "repository"
            root.mkdir()
            (base / "private").write_text("isolated")
            (root / "README.md").write_text("Text about its allowed component.")
            (root / ".env").write_text("private config")
            (root / ".git").mkdir()
            (root / ".git" / "config").write_text("private metadata")
            (root / ".github").mkdir()
            (root / ".github" / "info").write_text("workflow evidence")
            (root / "escape").symlink_to(base / "private")
            (root / "escape-dir").symlink_to(base, target_is_directory=True)
            with EvidenceReader({"repo": root}) as reader:
                proof = reader.read("repo", "README.md")
                self.assertEqual(proof.path, "README.md")
                self.assertTrue(proof.revision.startswith("sha256:"))
                self.assertEqual(reader.search("repo", "component", ("README.md",)).records, (proof,))
                self.assertEqual(reader.read("repo", ".github/info").text, "workflow evidence")
                for repo, path in (
                    ("foreign", "README.md"),
                    ("repo", "../private"),
                    ("repo", "/etc/passwd"),
                    ("repo", "escape"),
                    ("repo", "escape-dir/private"),
                    ("repo", ".env"),
                    ("repo", ".git/config"),
                    ("repo", "bad\0path"),
                    ("repo", "one//two"),
                ):
                    with self.subTest(path=path), self.assertRaises(EvidenceError):
                        reader.read(repo, path)

    def test_overlimit_file_fails_without_partial_bytes(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "README.md").write_text("x" * 100000)
            with EvidenceReader({"repo": root}) as reader:
                with self.assertRaises(EvidenceError) as failure:
                    reader.read("repo", "README.md")
                self.assertEqual(failure.exception.code, "file_limit")
                initial = reader.initial()
                self.assertEqual(initial.records, ())
                self.assertEqual(initial.omissions[0].reason, "file_limit")
