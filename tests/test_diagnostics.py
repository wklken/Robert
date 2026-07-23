from pathlib import Path
import tempfile
import unittest
import zipfile

from robert_agent.diagnostics import export_diagnostics


class DiagnosticsTests(unittest.TestCase):
    def test_export_excludes_prompts_and_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "config.yml"
            config.write_text(
                "github:\n  account: robert-bot\nsecret: should-not-export\n",
                encoding="utf-8",
            )
            output = root / "diagnostics.zip"
            result = export_diagnostics(config, output)
            self.assertTrue(result["ok"])
            with zipfile.ZipFile(output) as archive:
                names = archive.namelist()
                self.assertIn("manifest.json", names)
                content = b"".join(archive.read(name) for name in names)
            self.assertNotIn(b"should-not-export", content)
            self.assertNotIn(b"prompt", content.lower())
