import os
from pathlib import Path
import unittest
from unittest import mock

from robert_agent.paths import default_config_path, default_data_dir


class PathTests(unittest.TestCase):
    def test_default_paths_follow_robert_xdg_layout(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch.object(Path, "home", return_value=Path("/home/tester")):
                self.assertEqual(
                    default_config_path(),
                    Path("/home/tester/.config/robert/config.yml"),
                )
                self.assertEqual(
                    default_data_dir(),
                    Path("/home/tester/.local/share/robert"),
                )

    def test_environment_overrides_win(self):
        with mock.patch.dict(
            os.environ,
            {
                "ROBERT_CONFIG": "/tmp/robert-config.yml",
                "ROBERT_DATA_DIR": "/tmp/robert-data",
            },
            clear=True,
        ):
            self.assertEqual(default_config_path(), Path("/tmp/robert-config.yml"))
            self.assertEqual(default_data_dir(), Path("/tmp/robert-data"))
