from pathlib import Path
import tempfile
import unittest

from robert_agent.integrations.openclaw import write_plugin


class OpenClawTests(unittest.TestCase):
    def test_generated_plugin_uses_robert_cli_and_has_no_scheduler(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin_dir = Path(tmp) / "robert-openclaw"
            result = write_plugin(plugin_dir)
            self.assertTrue(result["ok"])
            source = (plugin_dir / "index.js").read_text(encoding="utf-8")
        self.assertIn('"robert"', source)
        self.assertIn('"status"', source)
        self.assertIn('"task"', source)
        self.assertNotIn("cron", source.lower())
        self.assertNotIn("run_once", source)
