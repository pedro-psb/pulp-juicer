"""Checks for observation and run weighting in interactive summaries."""

import math
import unittest

from pulp_juicer.summary_statistics import distribution, summarize_candidate


class TestSummaryStatistics(unittest.TestCase):
    def test_event_percentiles_and_population_sd(self):
        summary = distribution([1, 2, 4, 8], total=15)
        self.assertEqual(summary["n"], 4)
        self.assertEqual(summary["mean"], 3.75)
        self.assertEqual(summary["median"], 3)
        self.assertAlmostEqual(summary["p95"], 7.4)
        self.assertEqual(summary["total"], 15)
        self.assertAlmostEqual(summary["sd"], math.sqrt(7.1875))

    def test_collector_time_occupancy(self):
        summary = distribution([10, 30], weights=[1, 3], total=100)
        self.assertEqual(summary["n"], 2)
        self.assertEqual(summary["mean"], 25)
        self.assertEqual(summary["median"], 30)
        self.assertEqual(summary["p95"], 30)
        self.assertAlmostEqual(summary["sd"], math.sqrt(75))

    def test_candidate_gives_each_run_equal_weight_and_omits_missing(self):
        runs = [
            {"statistics": {"request_latency": {"n": 100, "mean": 10}}},
            {"statistics": {"request_latency": {"n": 1, "mean": 30}}},
            {"statistics": {}},
        ]
        summary = summarize_candidate(runs)["request_latency"]
        self.assertEqual(summary["mean"]["n"], 2)
        self.assertEqual(summary["mean"]["mean"], 20)
        self.assertAlmostEqual(summary["mean"]["sd"], math.sqrt(200))
        self.assertEqual(summary["n"]["mean"], 50.5)


if __name__ == "__main__":
    unittest.main()
