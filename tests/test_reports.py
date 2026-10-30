"""The report discovers both Make results and experiment plan results."""

import tempfile
import unittest
from pathlib import Path

from generate_reports import completed_runs


class TestReportDiscovery(unittest.TestCase):
    def test_discovers_both_result_layouts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = root / "normal" / "run001"
            new = root / "burst" / "normal" / "run002"
            hidden = root / "_sources" / "normal" / "run003"
            for directory in (old, new, hidden):
                directory.mkdir(parents=True)
                (directory / "locust_stats.csv").write_text("Name\nAggregated\n")
            self.assertEqual(set(completed_runs(root)), {old, new})


if __name__ == "__main__":
    unittest.main()
