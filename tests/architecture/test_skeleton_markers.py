import unittest

from tests.support import PACKAGE_ROOT, REPO_ROOT


class SkeletonMarkersTests(unittest.TestCase):
    def test_no_skeleton_markers_remain(self):
        hits = []
        for path in (PACKAGE_ROOT).rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if "stage_2_skeleton" in text or "skeleton_main" in text:
                hits.append(str(path.relative_to(REPO_ROOT)))
        self.assertEqual(hits, [])
