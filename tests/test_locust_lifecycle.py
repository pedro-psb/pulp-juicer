"""The Python wrapper brackets each Locust measurement."""

import csv
import hashlib
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import gevent
from locust import User, task
from locust.env import Environment
from locust.event import Events
from locust.runners import STATE_RUNNING

from pulp_juicer.locust_driver import _start_step_load, run
from pulp_juicer.plan import RunMetadata
from pulp_juicer.types import LoadStep, StepFunctionLoad, UnitaryImpulseLoad


class TestLocustLifecycle(unittest.TestCase):
    def test_step_schedule_works_with_locust_runner(self):
        class QuietUser(User):
            @task
            def idle(self):
                gevent.sleep(0.2)

        runner = Environment(
            user_classes=[QuietUser], events=Events()
        ).create_local_runner()
        load = StepFunctionLoad(
            steps=(LoadStep(0, 0), LoadStep(0.02, 1)),
            max_duration="1s",
        )
        try:
            scheduled = _start_step_load(runner, load, lambda: False)
            scheduled.join(timeout=1)
            gevent.sleep(0.05)
            self.assertEqual(runner.user_count, 1)
        finally:
            runner.quit()

    def test_step_schedule_changes_target_user_count(self):
        calls = []
        runner = SimpleNamespace(
            start=lambda users, rate: calls.append((users, rate)),
            state=STATE_RUNNING,
        )
        load = StepFunctionLoad(
            steps=(LoadStep(0, 1), LoadStep(0.01, 3)),
            max_duration="2s",
        )
        scheduled = _start_step_load(runner, load, lambda: False)
        scheduled.join(timeout=1)
        self.assertTrue(scheduled.dead)
        self.assertEqual(calls, [(1, 1), (3, 2)])

    def test_step_function_starts_users_at_scheduled_step(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
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
                    "smoke",
                    "small",
                    SimpleNamespace(id=1, name="normal"),
                    "run001",
                    {},
                    (),
                    StepFunctionLoad(
                        steps=(LoadStep(0, 0), LoadStep(0.05, 1)),
                        max_duration="2s",
                        idle_before_seconds=0,
                        tail_after_seconds=0,
                    ),
                    "http_get",
                    lambda *args: {},
                    Path(tmp) / "result",
                )
                result = run(
                    metadata,
                    lambda: {
                        "base_url": f"http://127.0.0.1:{server.server_port}",
                        "requests": [{"path": "artifact"}],
                    },
                    lambda: None,
                    monitor_cpu=False,
                )
                self.assertEqual(result.completed_requests, 1)
                self.assertFalse(result.timed_out)
        finally:
            server.shutdown()
            server.server_close()

    def test_failed_request_keeps_results(self):
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
                    "smoke",
                    "small",
                    SimpleNamespace(id=1, name="normal"),
                    "run001",
                    {},
                    (),
                    UnitaryImpulseLoad(
                        request_count=1,
                        max_duration="5s",
                        idle_before_seconds=0,
                        tail_after_seconds=0,
                    ),
                    "http_get",
                    lambda *args: {},
                    Path(tmp) / "result",
                )
                result = run(
                    metadata,
                    lambda: {
                        "base_url": f"http://127.0.0.1:{server.server_port}",
                        "requests": [{"path": "broken", "expected_status": 200}],
                    },
                    lambda: None,
                    monitor_cpu=False,
                )
                self.assertEqual(result.failures, 1)
                self.assertEqual(result.completed_requests, 1)
                self.assertTrue((metadata.result_dir / "locust_failures.csv").is_file())
        finally:
            server.shutdown()
            server.server_close()

    def test_completed_run_with_failures_keeps_artifacts_without_socket(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = RunMetadata(
                "smoke",
                "small",
                SimpleNamespace(id=1, name="normal"),
                "run001",
                {},
                (),
                UnitaryImpulseLoad(request_count=1, max_duration="5s"),
                "http_get",
                lambda *args: {},
                Path(tmp) / "result",
            )
            result = SimpleNamespace(
                failures=1,
                timed_out=False,
                completed_requests=1,
                expected_requests=1,
            )
            with patch("pulp_juicer.locust_driver._run_created", return_value=result):
                actual = run(metadata, dict, lambda: None)
            self.assertIs(actual, result)
            self.assertTrue(metadata.result_dir.is_dir())

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
                    "smoke",
                    "small",
                    SimpleNamespace(id=1, name="normal"),
                    "run001",
                    {},
                    (),
                    UnitaryImpulseLoad(
                        request_count=2,
                        max_duration="10s",
                        idle_before_seconds=0,
                        tail_after_seconds=0,
                    ),
                    "http_get",
                    lambda *args: {},
                    Path(tmp) / "result",
                )

                def start():
                    events.append("start")
                    self.assertFalse(
                        (metadata.result_dir / "locust_stats_history.csv").exists()
                    )
                    return {
                        "base_url": f"http://127.0.0.1:{server.server_port}",
                        "requests": [
                            {
                                "path": str(index),
                                "sha256": hashlib.sha256(b"fixture").hexdigest(),
                            }
                            for index in range(2)
                        ],
                    }

                progress = []

                def stop():
                    self.assertTrue(
                        (metadata.result_dir / "locust_stats.csv").is_file()
                    )
                    self.assertTrue(
                        (metadata.result_dir / "locust_stats_history.csv").is_file()
                    )
                    events.append("stop")

                result = run(
                    metadata,
                    start,
                    stop,
                    monitor_cpu=False,
                    on_progress=progress.append,
                    progress_interval_seconds=0.05,
                )
                self.assertEqual(result.completed_requests, 2)
                self.assertEqual(result.failures, 0)
                self.assertIsNotNone(result.load_start_utc)
                self.assertEqual(events, ["start", "request", "request", "stop"])
                self.assertIn("Preparing service and requests...", progress)
                self.assertTrue(
                    any("Locust: 2/2 requests" in line for line in progress)
                )
                self.assertGreaterEqual(
                    sum(" requests," in line for line in progress), 3
                )
                with (metadata.result_dir / "data_load.csv").open(newline="") as stream:
                    data_load = list(csv.DictReader(stream))
                self.assertEqual([row["path"] for row in data_load], ["0", "1"])
                self.assertEqual(
                    [row["url"] for row in data_load],
                    [
                        f"http://127.0.0.1:{server.server_port}/{index}"
                        for index in range(2)
                    ],
                )
                self.assertTrue(
                    all(
                        row["sha256"] == hashlib.sha256(b"fixture").hexdigest()
                        for row in data_load
                    )
                )
                self.assertFalse((metadata.result_dir / "scenario_input.json").exists())
                self.assertFalse((metadata.result_dir / "run.json").exists())
                self.assertFalse((metadata.result_dir / "run_result.json").exists())
        finally:
            server.shutdown()
            server.server_close()

    def test_teardown_runs_when_setup_fails(self):
        events = []
        with tempfile.TemporaryDirectory() as tmp:
            metadata = RunMetadata(
                "smoke",
                "small",
                SimpleNamespace(id=1, name="normal"),
                "run001",
                {},
                (),
                UnitaryImpulseLoad(
                    request_count=1,
                    max_duration="2s",
                    idle_before_seconds=0,
                    tail_after_seconds=0,
                ),
                "http_get",
                lambda *args: {},
                Path(tmp) / "result",
            )

            def start():
                events.append("start")
                raise ValueError("setup failed")

            with (
                patch(
                    "pulp_juicer.locust_driver.Environment.create_local_runner"
                ) as create,
                self.assertRaisesRegex(ValueError, "setup failed"),
            ):
                run(metadata, start, lambda: events.append("stop"), monitor_cpu=False)
            create.assert_not_called()
            self.assertEqual(events, ["start", "stop"])
            self.assertFalse(metadata.result_dir.exists())

    def test_failed_run_keeps_only_compose_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "result"
            metadata = RunMetadata(
                "smoke",
                "small",
                SimpleNamespace(id=1, name="normal"),
                "run001",
                {},
                (),
                UnitaryImpulseLoad(request_count=1, max_duration="2s"),
                "http_get",
                lambda *args: {},
                output,
            )

            def start():
                (output / "partial.csv").write_text("incomplete")
                raise ValueError("setup failed")

            def stop():
                (output / "compose.log").write_text("pulp | startup error\n")

            with self.assertRaisesRegex(ValueError, "setup failed"):
                run(metadata, start, stop, monitor_cpu=False)

            self.assertEqual([path.name for path in output.iterdir()], ["compose.log"])
            self.assertEqual(
                (output / "compose.log").read_text(), "pulp | startup error\n"
            )


if __name__ == "__main__":
    unittest.main()
