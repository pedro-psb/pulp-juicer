"""Result normalization keeps raw exports and gives reports typed Parquet inputs."""

import tempfile
import unittest
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from pulp_juicer.execution_timing import read_execution_timings
from pulp_juicer.result_data import normalize_execution, read_rows


class TestResultData(unittest.TestCase):
    def test_migrates_legacy_measurements_and_graphs(self):
        with tempfile.TemporaryDirectory() as temporary:
            execution = Path(temporary)
            run = execution / "burst" / "normal" / "run001"
            run.mkdir(parents=True)
            (run / "locust_stats.csv").write_text(
                "Type,Name,Request Count,Failure Count,Requests/s\n"
                "GET,Aggregated,2,0,1.5\n"
            )
            (run / "locust_failures.csv").write_text(
                "Method,Name,Error,Occurrences,First Seen,Last Seen\n"
            )
            (run / "requests.csv").write_text(
                "started_at_utc,completed_at_utc,success\n"
                "2026-01-01T00:00:00+00:00,2026-01-01T00:00:01+00:00,True\n"
            )
            (run / "report_completed.svg").write_text("<svg/>")
            (execution / "trials_completed.svg").write_text("<svg/>")
            (execution / "execution.log").write_text(
                "2026-01-01T00:00:00Z INFO Starting burst/run001 normal\n"
                "2026-01-01T00:00:01Z INFO Pre-test hooks finished\n"
                "2026-01-01T00:00:02Z INFO Load test started\n"
                "2026-01-01T00:00:05Z INFO Load test stopped\n"
                "2026-01-01T00:00:06Z INFO Post-test hooks started\n"
                "2026-01-01T00:00:07Z INFO Finished burst/run001 normal: 2/2 requests\n"
            )

            normalize_execution(execution)
            self.assertEqual(read_rows(run, "locust_stats")[0]["Request Count"], 2)
            self.assertEqual(
                pq.read_schema(run / "locust_stats.parquet").field("Requests/s").type,
                pa.float64(),
            )
            self.assertEqual(
                pq.read_schema(run / "locust_failures.parquet")
                .field("Occurrences")
                .type,
                pa.int64(),
            )
            self.assertTrue((run / "raw" / "locust_stats.csv").is_file())
            self.assertTrue((run / "raw" / "requests.csv").is_file())
            self.assertTrue((run / "graphs" / "report_completed.svg").is_file())
            self.assertTrue((execution / "graphs" / "trials_completed.svg").is_file())
            self.assertTrue((run / "requests.parquet").is_file())
            timings = read_execution_timings(execution)
            self.assertEqual(timings[("burst", "normal", "run001")].total_s, 7.0)
            self.assertEqual(
                timings[("burst", "normal", "run001")].durations_s["Load test"], 3.0
            )

            normalize_execution(execution)
            self.assertEqual(len(read_rows(run, "locust_stats")), 1)
