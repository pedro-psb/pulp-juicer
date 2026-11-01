"""Execution log phase timing uses matched trial markers."""

import tempfile
import unittest
from pathlib import Path

from pulp_juicer.execution_timing import parse_execution_timings


class TestExecutionTiming(unittest.TestCase):
    def test_missing_phase_keeps_total_and_unfinished_trial_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "execution.log"
            path.write_text(
                "2026-01-01T00:00:00Z INFO Starting burst/run001 normal\n"
                "2026-01-01T00:00:04Z INFO Pre-test hooks finished\n"
                "2026-01-01T00:00:06Z INFO Load test started\n"
                "2026-01-01T00:00:10Z INFO Post-test hooks started\n"
                "2026-01-01T00:00:15Z INFO Finished burst/run001 normal: 1/1 requests\n"
                "2026-01-01T00:00:16Z INFO Starting burst/run002 staging\n"
                "2026-01-01T00:00:17Z INFO Load test started\n"
            )
            timings = parse_execution_timings(path)
            self.assertEqual(set(timings), {("burst", "normal", "run001")})
            timing = timings[("burst", "normal", "run001")]
            self.assertEqual(timing.total_s, 15)
            self.assertEqual(timing.durations_s["Preparation"], 4)
            self.assertEqual(timing.durations_s["Locust startup"], 2)
            self.assertIsNone(timing.durations_s["Load test"])
            self.assertIsNone(timing.durations_s["Locust shutdown"])
            self.assertEqual(timing.durations_s["Cleanup"], 5)


if __name__ == "__main__":
    unittest.main()
