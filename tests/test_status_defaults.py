import unittest
from pathlib import Path

from robert_agent import status


class StatusDefaultsTests(unittest.TestCase):
    def test_default_database_uses_current_name(self):
        self.assertEqual(Path(status.DEFAULT_DB).name, "robert.sqlite3")
