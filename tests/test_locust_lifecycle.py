"""Locust lifecycle hooks bracket an individual measurement."""

import csv
import hashlib
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

from pulp_juicer.locust_driver import run
from pulp_juicer.plan import RunMetadata
from pulp_juicer.types import LoadConfig


class TestLocustLifecycle(unittest.TestCase):
    def test_failed_request_removes_results(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(500)
                self.end_headers()

            def log_message(self, *args):
                pass

        try:
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        except PermissionError:
            self.skipTest("Local socket binding is restricted")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                metadata = RunMetadata(
                    "smoke", "small", SimpleNamespace(id=1, name="normal"), "run001", {}, (),
                    LoadConfig(users=1, spawn_rate_per_second=1, max_duration="5s",
                               idle_before_seconds=0, tail_after_seconds=0),
                    "http_get", lambda *args: {}, True, Path(tmp) / "result",
                )
                result = run(
                    metadata,
                    lambda: {"base_url": f"http://127.0.0.1:{server.server_port}",
                             "requests": [{"path": "broken", "expected_status": 200}]},
                    lambda: None,
                    monitor_cpu=False,
                )
                self.assertEqual(result.failures, 1)
                self.assertFalse(metadata.result_dir.exists())
        finally:
            server.shutdown()
            server.server_close()

    def test_start_before_requests_and_stop_after(self):
        events = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                events.append("request")
                time.sleep(0.2)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"fixture")

            def log_message(self, *args):
                pass

        try:
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        except PermissionError:
            self.skipTest("Local socket binding is restricted")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                metadata = RunMetadata(
                    "smoke", "small", SimpleNamespace(id=1, name="normal"), "run001", {}, (),
                    LoadConfig(users=2, spawn_rate_per_second=2, max_duration="10s",
                               idle_before_seconds=0, tail_after_seconds=0),
                    "http_get", lambda *args: {}, True, Path(tmp) / "result",
                )

                def start():
                    events.append("start")
                    return {
                        "base_url": f"http://127.0.0.1:{server.server_port}",
                        "requests": [
                            {"path": str(index), "sha256": hashlib.sha256(b"fixture").hexdigest()}
                            for index in range(2)
                        ],
                    }

                progress = []
                result = run(
                    metadata, start, lambda: events.append("stop"), monitor_cpu=False,
                    on_progress=progress.append, progress_interval_seconds=0.05,
                )
                self.assertEqual(result.completed_requests, 2)
                self.assertEqual(result.failures, 0)
                self.assertEqual(events, ["start", "request", "request", "stop"])
                self.assertIn("Locust: preparing service and requests...", progress)
                self.assertTrue(any("Locust: 2/2 requests" in line for line in progress))
                self.assertGreaterEqual(sum(" requests," in line for line in progress), 3)
                with (metadata.result_dir / "data_load.csv").open(newline="") as stream:
                    data_load = list(csv.DictReader(stream))
                self.assertEqual([row["path"] for row in data_load], ["0", "1"])
                self.assertEqual(
                    [row["url"] for row in data_load],
                    [f"http://127.0.0.1:{server.server_port}/{index}" for index in range(2)],
                )
                self.assertTrue(all(row["sha256"] == hashlib.sha256(b"fixture").hexdigest() for row in data_load))
                self.assertFalse((metadata.result_dir / "scenario_input.json").exists())
                self.assertFalse((metadata.result_dir / "run.json").exists())
                self.assertFalse((metadata.result_dir / "run_result.json").exists())
        finally:
            server.shutdown()
            server.server_close()

    def test_stop_hook_runs_when_start_hook_fails(self):
        events = []
        with tempfile.TemporaryDirectory() as tmp:
            metadata = RunMetadata(
                "smoke", "small", SimpleNamespace(id=1, name="normal"), "run001", {}, (),
                LoadConfig(users=1, spawn_rate_per_second=1, max_duration="2s",
                           idle_before_seconds=0, tail_after_seconds=0),
                "http_get", lambda *args: {}, True, Path(tmp) / "result",
            )

            def start():
                events.append("start")
                raise ValueError("setup failed")

            with self.assertRaisesRegex(ValueError, "setup failed"):
                run(metadata, start, lambda: events.append("stop"), monitor_cpu=False)
            self.assertEqual(events, ["start", "stop"])
            self.assertFalse(metadata.result_dir.exists())


if __name__ == "__main__":
    unittest.main()
