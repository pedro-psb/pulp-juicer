"""Execution passes each planned run to the Locust driver in order."""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pulp_juicer.compose_runtime import ComposeRuntime
from pulp_juicer.execution import _write_run_context, execute_test_plan
from pulp_juicer.plan import RunMetadata, RunResultMetadata
from pulp_juicer.types import (
    PreTestFunction,
    PulpConnection,
    ServiceFixtureMetadata,
    UnitaryImpulseLoad,
)


def pre_test_functions():
    return [
        PreTestFunction(
            "Create test resources",
            Mock(return_value={"base_url": "http://example.test/"}),
        )
    ]


class TestExecution(unittest.TestCase):
    def test_post_debug_keeps_services_after_success_or_test_failure(self):
        for fail in (False, True):
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                output = root / "run001"
                candidate = SimpleNamespace(
                    name="normal",
                    sources={},
                    pre_test_functions=pre_test_functions(),
                )
                run = SimpleNamespace(
                    result_dir=output,
                    scenario_id="small",
                    run_id="run001",
                    candidate=candidate,
                    fixtures={},
                    fixture_builds=(),
                    load=SimpleNamespace(),
                    input_builder=Mock(return_value={"requests": [{"path": "a"}]}),
                )
                plan = SimpleNamespace(
                    execution_id="smoke__small__run001__001",
                    runs=(run,),
                    experiment=SimpleNamespace(required_sources=()),
                    repository_root=root,
                    results_dir=root,
                    pulp_connection=PulpConnection(),
                    compose_command=("docker", "compose"),
                    compose_file=root / "pulp.yaml",
                )

                def fake_locust(
                    run, start, stop, *, output=output, fail=fail, **kwargs
                ):
                    output.mkdir()
                    start()
                    stop()
                    if fail:
                        raise RuntimeError("load failed")
                    return SimpleNamespace(
                        timed_out=False,
                        completed_requests=1,
                        expected_requests=1,
                        failures=0,
                        result_dir=output,
                        scenario_id="small",
                        run_id="run001",
                        candidate_name="normal",
                    )

                with (
                    patch("pulp_juicer.execution.ComposeRuntime") as runtime_type,
                    patch("pulp_juicer.execution.PulpClient.get", return_value={}),
                    patch("pulp_juicer.execution.run_locust", side_effect=fake_locust),
                    patch("pulp_juicer.execution._write_run_context"),
                ):
                    if fail:
                        with self.assertRaisesRegex(RuntimeError, "load failed"):
                            execute_test_plan(plan, post_debug=True)
                    else:
                        self.assertEqual(
                            len(execute_test_plan(plan, post_debug=True)), 1
                        )
                runtime = runtime_type.return_value
                runtime.ensure.assert_called_once()
                runtime.dump_logs.assert_called_once()
                runtime.close.assert_not_called()

    def test_post_debug_rejects_multiple_runs_before_service_work(self):
        with (
            patch("pulp_juicer.execution.ComposeRuntime") as runtime_type,
            self.assertRaisesRegex(ValueError, "exactly one run"),
        ):
            execute_test_plan(SimpleNamespace(runs=(1, 2)), post_debug=True)
        runtime_type.assert_not_called()

    def test_run_metadata_contains_configuration_setup_and_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture_dir = root / "fixture"
            fixture_dir.mkdir()
            (fixture_dir / "manifest.json").write_text(
                json.dumps({"artifacts": [{"path": "a", "sha256": "abc"}]})
            )
            output = root / "result"
            output.mkdir()
            candidate = SimpleNamespace(
                id=1,
                name="normal",
                sources={"pulpcore": "../pulpcore", "pulp_maven": "../pulp_maven"},
                pre_test_functions=pre_test_functions(),
            )
            fixture = ServiceFixtureMetadata(
                name="maven",
                compose_file=root / "fixture.yaml",
                service_name="fixture",
                output_dir=fixture_dir,
                url="http://fixture/",
            )
            run = RunMetadata(
                "smoke",
                "small",
                candidate,
                "run001",
                {"maven": fixture},
                (SimpleNamespace(count=1, size=1024),),
                UnitaryImpulseLoad(request_count=1, max_duration="5s"),
                "http_get",
                lambda *args: {},
                output,
            )
            result = RunResultMetadata(
                "smoke",
                "small",
                1,
                "normal",
                "run001",
                output,
                1,
                1,
                0,
                12.0,
                20.0,
                False,
                "2026-01-01T00:00:00+00:00",
            )
            with patch(
                "pulp_juicer.execution.source_details", return_value={"revision": "abc"}
            ):
                _write_run_context(
                    run,
                    result,
                    {"base_url": "http://pulp/"},
                    {"pulpcore": root, "pulp_maven": root},
                    "smoke",
                    "smoke__small__run001",
                )

            self.assertEqual(
                {path.name for path in output.iterdir()}, {"run_metadata.json"}
            )
            self.assertTrue((fixture_dir / "manifest.json").is_file())
            metadata = json.loads((output / "run_metadata.json").read_text())
            self.assertEqual(metadata["experiment"], "smoke")
            self.assertEqual(metadata["execution_id"], "smoke__small__run001")
            self.assertEqual(metadata["candidate_id"], 1)
            self.assertEqual(metadata["pre_test_functions"], ["Create test resources"])
            self.assertEqual(metadata["pulp_settings"], {})
            self.assertEqual(metadata["load"]["request_count"], 1)
            self.assertEqual(metadata["load"]["profile"], "UnitaryImpulseLoad")
            self.assertEqual(metadata["load"]["request_timeout_seconds"], 60)
            self.assertEqual(metadata["setup"]["base_url"], "http://pulp/")
            self.assertEqual(metadata["result"]["p95_response_ms"], 20.0)
            self.assertEqual(
                metadata["result"]["load_start_utc"], "2026-01-01T00:00:00+00:00"
            )
            self.assertEqual(metadata["fixture"]["count"], 1)
            self.assertEqual(set(metadata["sources"]), {"pulpcore", "pulp_maven"})

    def test_report_metadata_failure_removes_new_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "results"
            candidate = SimpleNamespace(
                id=1,
                name="normal",
                sources={"pulpcore": "../pulpcore", "pulp_maven": "../pulp_maven"},
                pre_test_functions=pre_test_functions(),
            )
            run = RunMetadata(
                "smoke",
                "small",
                candidate,
                "run001",
                {},
                (),
                UnitaryImpulseLoad(request_count=1, max_duration="1s"),
                "http_get",
                lambda *args: {
                    "base_url": "http://example.test/",
                    "requests": [{"path": "a"}],
                },
                output,
            )
            plan = SimpleNamespace(
                execution_id="smoke__small__run001",
                runs=(run,),
                experiment=SimpleNamespace(required_sources=("pulpcore", "pulp_maven")),
                repository_root=root,
                results_dir=root,
                pulp_connection=PulpConnection(),
                compose_command=("docker", "compose"),
                compose_file=root / "pulp.yaml",
            )

            def fake_locust(run, start, stop, **kwargs):
                output.mkdir()
                start()
                stop()
                return SimpleNamespace(
                    timed_out=False,
                    failures=0,
                    completed_requests=1,
                    expected_requests=1,
                    result_dir=output,
                )

            with (
                patch("pulp_juicer.execution.ComposeRuntime"),
                patch("pulp_juicer.execution.PulpClient.get", return_value={}),
                patch(
                    "pulp_juicer.execution.resolve_source", return_value=root / "source"
                ),
                patch("pulp_juicer.execution.run_locust", side_effect=fake_locust),
                patch(
                    "pulp_juicer.execution._write_run_context",
                    side_effect=OSError("write failed"),
                ),
                self.assertRaisesRegex(OSError, "write failed"),
            ):
                execute_test_plan(plan)
            self.assertFalse(output.exists())

    def test_existing_results_skip_before_any_service_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "results"
            output.mkdir()
            (output / "run_metadata.json").write_text("{}")
            run = SimpleNamespace(result_dir=output)
            skipped = []
            with (
                patch("pulp_juicer.execution.ComposeRuntime") as runtime_type,
                patch("pulp_juicer.execution.run_locust") as locust,
            ):
                results = execute_test_plan(
                    SimpleNamespace(runs=(run,)), on_skip=skipped.append
                )
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
                id=1,
                name="normal",
                sources={"pulpcore": "../pulpcore", "pulp_maven": "../pulp_maven"},
                pre_test_functions=pre_test_functions(),
            )
            run = RunMetadata(
                "smoke",
                "small",
                candidate,
                "run001",
                {},
                (),
                UnitaryImpulseLoad(request_count=1, max_duration="1s"),
                "http_get",
                lambda *args: {
                    "base_url": "http://example.test/",
                    "requests": [{"path": "a"}],
                },
                output,
            )
            plan = SimpleNamespace(
                execution_id="smoke__small__run001",
                runs=(run,),
                experiment=SimpleNamespace(required_sources=("pulpcore", "pulp_maven")),
                repository_root=root,
                results_dir=root,
                pulp_connection=PulpConnection(),
                compose_command=("docker", "compose"),
                compose_file=root / "pulp.yaml",
            )
            progress = []

            def fake_locust(run, start, stop, **kwargs):
                self.assertFalse(output.exists())
                output.mkdir()
                start()
                stop()
                return SimpleNamespace(
                    timed_out=False,
                    failures=0,
                    completed_requests=1,
                    expected_requests=1,
                    result_dir=output,
                )

            with (
                patch("pulp_juicer.execution.ComposeRuntime") as runtime_type,
                patch("pulp_juicer.execution.PulpClient.get", return_value={}),
                patch(
                    "pulp_juicer.execution.resolve_source", return_value=root / "source"
                ),
                patch("pulp_juicer.execution.run_locust", side_effect=fake_locust),
                patch("pulp_juicer.execution._write_run_context"),
            ):
                results = execute_test_plan(
                    plan, force=True, on_progress=progress.append
                )
            self.assertEqual(len(results), 1)
            self.assertTrue(output.is_dir())
            self.assertFalse((output / "old-result.txt").exists())
            runtime_type.return_value.destroy_before_start.assert_called_once_with(
                reset=True
            )
            self.assertIn(f"Replacing existing results at {output}", progress)

    def test_pre_start_destroy_is_repeatable_before_services_start(self):
        plan = SimpleNamespace(
            experiment=SimpleNamespace(id="smoke"),
            repository_root=Path("/tmp"),
            compose_file=Path("/tmp/compose.yaml"),
            pulp_connection=PulpConnection(base_url="http://127.0.0.1:5002"),
        )
        runtime = ComposeRuntime(plan)
        runtime.command = Mock()
        runtime.destroy_before_start()
        runtime.destroy_before_start(reset=True)
        self.assertEqual(runtime.command.call_count, 2)
        self.assertEqual(
            runtime.command.call_args_list[0].args[2:], ("down", "--remove-orphans")
        )
        self.assertEqual(
            runtime.command.call_args_list[1].args[2:],
            ("down", "--remove-orphans", "--volumes"),
        )
        self.assertEqual(runtime.command.call_args_list[0].args[1]["PULP_PORT"], "5002")

    def test_shared_fixture_builds_once_and_every_run_resets_service(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = SimpleNamespace(
                name="fixture", output_dir=root / "fixture", build=Mock()
            )
            details = ServiceFixtureMetadata(
                name="fixture",
                compose_file=root / "fixture.yaml",
                service_name="fixture",
                output_dir=fixture.output_dir,
                url="http://fixture/",
                environment={"FIXTURE_DIR": str(fixture.output_dir)},
            )
            candidate = SimpleNamespace(
                id=1,
                name="normal",
                sources={"pulpcore": "../pulpcore", "pulp_maven": "../pulp_maven"},
                pre_test_functions=pre_test_functions(),
            )
            candidate.pre_test_functions.append(
                PreTestFunction(
                    "Add candidate marker", Mock(return_value={"marker": 1})
                )
            )
            alternate = SimpleNamespace(
                id=2,
                name="alternate",
                sources={
                    "pulpcore": "../alternate-pulpcore",
                    "pulp_maven": "../pulp_maven",
                },
                pre_test_functions=pre_test_functions(),
            )
            load = UnitaryImpulseLoad(request_count=1, max_duration="1s")
            input_builder = Mock(
                return_value={
                    "base_url": "http://example.test/",
                    "requests": [{"path": "a"}],
                }
            )
            runs = tuple(
                RunMetadata(
                    "smoke",
                    "small",
                    candidate if number == 1 else alternate,
                    f"run{number:03d}",
                    {"fixture": details},
                    (fixture,),
                    load,
                    "http_get",
                    input_builder,
                    root / f"run{number:03d}",
                )
                for number in (1, 2)
            )
            plan = SimpleNamespace(
                execution_id="smoke__small",
                runs=runs,
                experiment=SimpleNamespace(required_sources=("pulpcore", "pulp_maven")),
                repository_root=root,
                results_dir=root,
                pulp_connection=PulpConnection(),
                compose_command=("docker", "compose"),
                compose_file=root / "pulp.yaml",
            )
            progress = []

            def fake_locust(run, start, stop, **kwargs):
                run.result_dir.mkdir()
                kwargs["on_progress"]("Locust: 0/1 requests")
                start()
                self.assertEqual(
                    runtime_type.return_value.destroy_before_start.call_count,
                    int(run.run_id.removeprefix("run")),
                )
                stop()
                self.assertTrue((run.result_dir / "compose.log").is_file())
                return SimpleNamespace(
                    run_id=run.run_id,
                    result_dir=run.result_dir,
                    timed_out=False,
                    failures=1 if run.run_id == "run001" else 0,
                    completed_requests=1,
                    expected_requests=1,
                )

            def fake_dump_logs(path):
                path.write_text("pulp | service output\n")
                return path

            with (
                patch("pulp_juicer.execution.ComposeRuntime") as runtime_type,
                patch(
                    "pulp_juicer.execution.resolve_source",
                    side_effect=lambda source, *args: root / source.removeprefix("../"),
                ) as resolve,
                patch(
                    "pulp_juicer.execution.run_locust", side_effect=fake_locust
                ) as locust,
                patch(
                    "pulp_juicer.execution.PulpClient.get",
                    return_value={
                        "versions": [{"component": "core", "version": "3.0"}]
                    },
                ) as get_status,
                patch("pulp_juicer.execution._write_run_context") as write_context,
            ):
                runtime_type.return_value.project = "smoke"
                runtime_type.return_value.dump_logs.side_effect = fake_dump_logs
                results = execute_test_plan(plan, on_progress=progress.append)
            self.assertEqual(
                [result.run_id for result in results], ["run001", "run002"]
            )
            fixture.build.assert_called_once()
            self.assertEqual(
                [
                    call.kwargs["reset"]
                    for call in runtime_type.return_value.destroy_before_start.call_args_list
                ],
                [True, True],
            )
            self.assertEqual(runtime_type.return_value.ensure.call_count, 2)
            self.assertEqual(locust.call_count, 2)
            self.assertEqual(write_context.call_count, 2)
            self.assertEqual(runtime_type.return_value.dump_logs.call_count, 2)
            self.assertEqual(get_status.call_count, 2)
            self.assertEqual(
                [call.args[0] for call in get_status.call_args_list],
                ["/pulp/api/v3/status/"] * 2,
            )
            for run in runs:
                self.assertEqual(
                    json.loads((run.result_dir / "pulp_status.json").read_text()),
                    {"versions": [{"component": "core", "version": "3.0"}]},
                )
            self.assertEqual([result.failures for result in results], [1, 0])
            self.assertEqual(resolve.call_count, 3)
            ensure_calls = runtime_type.return_value.ensure.call_args_list
            self.assertEqual(
                ensure_calls[0].args[1]["PULPCORE_REPO"], str(root / "pulpcore")
            )
            self.assertEqual(
                ensure_calls[1].args[1]["PULPCORE_REPO"],
                str(root / "alternate-pulpcore"),
            )
            self.assertEqual(runtime_type.return_value.close.call_count, 3)
            self.assertEqual(progress[0], "Starting small/run001 normal")
            self.assertIn("Starting small/run002 alternate", progress)
            self.assertEqual(progress.count("Pre-test hooks started"), 2)
            self.assertEqual(progress.count("Pre-test hooks finished"), 2)
            self.assertEqual(progress.count("Pre-test: Create test resources"), 2)
            self.assertEqual(progress.count("Pre-test: Add candidate marker"), 1)
            self.assertEqual(
                input_builder.call_args_list[0].args[1],
                {"base_url": "http://example.test/", "marker": 1},
            )
            self.assertEqual(progress.count("Post-test hooks started"), 2)
            self.assertEqual(progress.count("Post-test hooks finished"), 2)
            self.assertIn("Prepared fixture fixture", progress)
            self.assertIn("Skipping fixture fixture: already prepared", progress)

    def test_compose_runtime_dumps_all_service_logs_with_timestamps(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = SimpleNamespace(
                experiment=SimpleNamespace(id="smoke"),
                repository_root=root,
                compose_file=root / "compose.yaml",
                pulp_connection=PulpConnection(),
                compose_command=("docker", "compose"),
            )
            runtime = ComposeRuntime(plan)
            runtime.files = (plan.compose_file,)
            runtime.environment = {"PULP_PORT": "5001"}
            output = root / "compose.log"

            def fake_subprocess(command, **kwargs):
                kwargs["stdout"].write("pulp | 2026-01-01T00:00:00Z ready\n")
                self.assertEqual(kwargs["stderr"], subprocess.STDOUT)
                return SimpleNamespace(returncode=0)

            with patch(
                "pulp_juicer.compose_runtime.subprocess.run",
                side_effect=fake_subprocess,
            ) as command:
                runtime.dump_logs(output)

            self.assertEqual(
                command.call_args.args[0][-3:],
                ["logs", "--no-color", "--timestamps"],
            )
            self.assertEqual(output.read_text(), "pulp | 2026-01-01T00:00:00Z ready\n")

    def test_status_capture_failure_does_not_discard_completed_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "result"
            candidate = SimpleNamespace(
                id=1,
                name="normal",
                sources={},
                pre_test_functions=pre_test_functions(),
            )
            run = RunMetadata(
                "smoke",
                "small",
                candidate,
                "run001",
                {},
                (),
                UnitaryImpulseLoad(request_count=1, max_duration="1s"),
                "http_get",
                lambda *args: {
                    "base_url": "http://example.test/",
                    "requests": [{"path": "a"}],
                },
                output,
            )
            plan = SimpleNamespace(
                execution_id="smoke__small__run001",
                runs=(run,),
                experiment=SimpleNamespace(required_sources=()),
                repository_root=root,
                results_dir=root,
                pulp_connection=PulpConnection(),
                compose_command=("docker", "compose"),
                compose_file=root / "pulp.yaml",
            )

            def fake_locust(run, start, stop, **kwargs):
                output.mkdir()
                start()
                stop()
                return SimpleNamespace(
                    result_dir=output,
                    timed_out=False,
                    failures=0,
                    completed_requests=1,
                    expected_requests=1,
                )

            with (
                patch("pulp_juicer.execution.ComposeRuntime"),
                patch("pulp_juicer.execution.run_locust", side_effect=fake_locust),
                patch(
                    "pulp_juicer.execution.PulpClient.get",
                    side_effect=OSError("offline"),
                ),
                patch("pulp_juicer.execution._write_run_context"),
            ):
                results = execute_test_plan(plan)

            self.assertEqual(len(results), 1)
            self.assertTrue(output.is_dir())
            self.assertFalse((output / "pulp_status.json").exists())
            self.assertEqual(
                (output / "pulp_status_error.txt").read_text(), "OSError: offline\n"
            )


if __name__ == "__main__":
    unittest.main()
