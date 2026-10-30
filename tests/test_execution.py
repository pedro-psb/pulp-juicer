"""Execution passes each planned run to the Locust driver in order."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pulp_juicer.compose_runtime import ComposeRuntime
from pulp_juicer.execution import _write_run_context, execute_test_plan
from pulp_juicer.plan import RunMetadata, RunResultMetadata
from pulp_juicer.types import LoadConfig, PluginSource, PulpConnection, ServiceFixtureMetadata


class TestExecution(unittest.TestCase):
    def test_run_metadata_contains_configuration_setup_and_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture_dir = root / "fixture"
            fixture_dir.mkdir()
            (fixture_dir / "manifest.json").write_text(json.dumps({"artifacts": [{"path": "a", "sha256": "abc"}]}))
            output = root / "result"
            output.mkdir()
            candidate = SimpleNamespace(
                id=1, name="normal", source=PluginSource(plugin="pulp_maven", git="../pulp_maven"),
            )
            fixture = ServiceFixtureMetadata(
                name="maven", compose_file=root / "fixture.yaml", service_name="fixture",
                output_dir=fixture_dir, url="http://fixture/",
            )
            run = RunMetadata(
                "smoke", "small", candidate, "run001", {"maven": fixture},
                (SimpleNamespace(count=1, size=1024),),
                LoadConfig(users=2, spawn_rate_per_second=2, max_duration="5s", request_count=1),
                "http_get", lambda *args: {}, True, output,
            )
            result = RunResultMetadata(
                "smoke", "small", 1, "normal", "run001", output,
                1, 1, 0, 12.0, 20.0, False,
            )
            plan = SimpleNamespace(pulpcore_source="../pulpcore")
            with patch("pulp_juicer.execution.source_details", return_value={"revision": "abc"}):
                _write_run_context(plan, run, result, {"base_url": "http://pulp/"}, root, root, "smoke")

            self.assertEqual({path.name for path in output.iterdir()}, {"run_metadata.json"})
            self.assertTrue((fixture_dir / "manifest.json").is_file())
            metadata = json.loads((output / "run_metadata.json").read_text())
            self.assertEqual(metadata["experiment"], "smoke")
            self.assertEqual(metadata["candidate_id"], 1)
            self.assertEqual(metadata["load"]["request_count"], 1)
            self.assertEqual(metadata["load"]["request_timeout_seconds"], 60)
            self.assertEqual(metadata["setup"]["base_url"], "http://pulp/")
            self.assertEqual(metadata["result"]["p95_response_ms"], 20.0)
            self.assertEqual(metadata["fixture"]["count"], 1)

    def test_report_metadata_failure_removes_new_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "results"
            candidate = SimpleNamespace(
                id=1, name="normal", source=PluginSource(plugin="pulp_maven", git="../pulp_maven"),
                setup=Mock(return_value={"base_url": "http://example.test/"}),
            )
            run = RunMetadata(
                "smoke", "small", candidate, "run001", {}, (),
                LoadConfig(users=1, spawn_rate_per_second=1, max_duration="1s"),
                "http_get", lambda *args: {"base_url": "http://example.test/", "requests": [{"path": "a"}]},
                False, output,
            )
            plan = SimpleNamespace(
                runs=(run,), repository_root=root, results_dir=root,
                pulpcore_source="../pulpcore", pulp_connection=PulpConnection(),
                compose_command=("docker", "compose"), compose_file=root / "pulp.yaml",
            )

            def fake_locust(run, start, stop, **kwargs):
                output.mkdir()
                start()
                stop()
                return SimpleNamespace(
                    timed_out=False, failures=0, completed_requests=1, expected_requests=1,
                    result_dir=output,
                )

            with patch("pulp_juicer.execution.ComposeRuntime"), \
                 patch("pulp_juicer.execution.resolve_source", return_value=root / "source"), \
                 patch("pulp_juicer.execution.run_locust", side_effect=fake_locust), \
                 patch("pulp_juicer.execution._write_run_context", side_effect=OSError("write failed")):
                with self.assertRaisesRegex(OSError, "write failed"):
                    execute_test_plan(plan)
            self.assertFalse(output.exists())

    def test_existing_results_skip_before_any_service_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "results"
            output.mkdir()
            run = SimpleNamespace(result_dir=output)
            skipped = []
            with patch("pulp_juicer.execution.ComposeRuntime") as runtime_type, \
                 patch("pulp_juicer.execution.run_locust") as locust:
                results = execute_test_plan(SimpleNamespace(runs=(run,)), on_skip=skipped.append)
            self.assertEqual(results, ())
            self.assertEqual(skipped, [run])
            runtime_type.return_value.destroy_before_start.assert_not_called()
            locust.assert_not_called()

    def test_force_replaces_existing_results_before_rerun(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "result"
            output.mkdir()
            (output / "old-result.txt").write_text("previous run")
            candidate = SimpleNamespace(
                id=1, name="normal", source=PluginSource(plugin="pulp_maven", git="../pulp_maven"),
                setup=Mock(return_value={"base_url": "http://example.test/"}),
            )
            run = RunMetadata(
                "smoke", "small", candidate, "run001", {}, (),
                LoadConfig(users=1, spawn_rate_per_second=1, max_duration="1s"),
                "http_get", lambda *args: {"base_url": "http://example.test/", "requests": [{"path": "a"}]},
                False, output,
            )
            plan = SimpleNamespace(
                runs=(run,), repository_root=root, results_dir=root,
                pulpcore_source="../pulpcore", pulp_connection=PulpConnection(),
                compose_command=("docker", "compose"), compose_file=root / "pulp.yaml",
            )
            progress = []

            def fake_locust(run, start, stop, **kwargs):
                self.assertFalse(output.exists())
                output.mkdir()
                start()
                stop()
                return SimpleNamespace(
                    timed_out=False, failures=0, completed_requests=1, expected_requests=1,
                    result_dir=output,
                )

            with patch("pulp_juicer.execution.ComposeRuntime") as runtime_type, \
                 patch("pulp_juicer.execution.resolve_source", return_value=root / "source"), \
                 patch("pulp_juicer.execution.run_locust", side_effect=fake_locust), \
                 patch("pulp_juicer.execution._write_run_context"):
                results = execute_test_plan(plan, force=True, on_progress=progress.append)
            self.assertEqual(len(results), 1)
            self.assertTrue(output.is_dir())
            self.assertFalse((output / "old-result.txt").exists())
            runtime_type.return_value.destroy_before_start.assert_called_once_with(reset=False)
            self.assertIn(f"Replacing existing results at {output}", progress)

    def test_pre_start_destroy_is_repeatable_before_services_start(self):
        plan = SimpleNamespace(
            experiment=SimpleNamespace(id="smoke"), repository_root=Path("/tmp"),
            compose_file=Path("/tmp/compose.yaml"),
            pulp_connection=PulpConnection(base_url="http://127.0.0.1:5002"),
        )
        runtime = ComposeRuntime(plan)
        runtime.command = Mock()
        runtime.destroy_before_start()
        runtime.destroy_before_start(reset=True)
        self.assertEqual(runtime.command.call_count, 2)
        self.assertEqual(runtime.command.call_args_list[0].args[2:], ("down", "--remove-orphans"))
        self.assertEqual(runtime.command.call_args_list[1].args[2:],
                         ("down", "--remove-orphans", "--volumes"))
        self.assertEqual(runtime.command.call_args_list[0].args[1]["PULP_PORT"], "5002")

    def test_shared_fixture_builds_once_and_restart_marker_is_honored(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = SimpleNamespace(output_dir=root / "fixture", build=Mock())
            details = ServiceFixtureMetadata(
                name="fixture", compose_file=root / "fixture.yaml", service_name="fixture",
                output_dir=fixture.output_dir, url="http://fixture/",
                environment={"FIXTURE_DIR": str(fixture.output_dir)},
            )
            candidate = SimpleNamespace(
                id=1, name="normal", source=PluginSource(plugin="pulp_maven", git="../pulp_maven"),
                setup=Mock(return_value={"base_url": "http://example.test/"}),
            )
            load = LoadConfig(users=1, spawn_rate_per_second=1, max_duration="1s")
            input_builder = Mock(return_value={"base_url": "http://example.test/", "requests": [{"path": "a"}]})
            runs = tuple(RunMetadata(
                "smoke", "small", candidate, f"run{number:03d}", {"fixture": details}, (fixture,),
                load, "http_get", input_builder, number == 1, root / f"run{number:03d}",
            ) for number in (1, 2))
            plan = SimpleNamespace(
                runs=runs, repository_root=root, results_dir=root,
                pulpcore_source="../pulpcore", pulp_connection=PulpConnection(),
                compose_command=("docker", "compose"), compose_file=root / "pulp.yaml",
            )
            progress = []
            def fake_locust(run, start, stop, **kwargs):
                self.assertEqual(runtime_type.return_value.destroy_before_start.call_count,
                                 int(run.run_id.removeprefix("run")))
                kwargs["on_progress"]("Locust: 0/1 requests")
                start()
                stop()
                return SimpleNamespace(
                    run_id=run.run_id, result_dir=run.result_dir, timed_out=False,
                    failures=0, completed_requests=1, expected_requests=1,
                )

            with patch("pulp_juicer.execution.ComposeRuntime") as runtime_type, \
                 patch("pulp_juicer.execution.resolve_source", return_value=root / "source") as resolve, \
                 patch("pulp_juicer.execution.run_locust", side_effect=fake_locust) as locust, \
                 patch("pulp_juicer.execution._write_run_context"):
                runtime_type.return_value.project = "smoke"
                results = execute_test_plan(plan, on_progress=progress.append)
            self.assertEqual([result.run_id for result in results], ["run001", "run002"])
            fixture.build.assert_called_once()
            self.assertEqual([call.kwargs["reset"] for call in runtime_type.return_value.destroy_before_start.call_args_list],
                             [True, False])
            self.assertEqual(runtime_type.return_value.ensure.call_count, 2)
            self.assertEqual(locust.call_count, 2)
            self.assertEqual(resolve.call_count, 2)
            self.assertEqual(runtime_type.return_value.close.call_count, 3)
            self.assertEqual(progress, [
                "Starting small/run001 normal", "Locust: 0/1 requests",
                "Starting small/run002 normal", "Locust: 0/1 requests",
            ])


if __name__ == "__main__":
    unittest.main()
