"""Checks for plan expansion and the read-only CLI path."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from click.testing import CliRunner

from experiments.maven_pull_through.experiment import EXPERIMENT
from pulp_juicer.cli import cli
from pulp_juicer.plan import build_test_plan
from pulp_juicer.plan_file import load_plan_file


COMPOSE = Path(__file__).resolve().parents[1] / "src/experiments/maven_pull_through/compose.yaml"
TOTAL_RUNS = sum(isinstance(entry, int) for scenario in EXPERIMENT.scenarios for entry in scenario.runs)


class TestPlan(unittest.TestCase):
    def test_expands_order_and_restart_markers_without_building_fixture(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = build_test_plan(EXPERIMENT, COMPOSE, repository_root=root, results_dir=root / "results")
            self.assertEqual(len(plan.runs), TOTAL_RUNS)
            self.assertEqual([run.restart_before for run in plan.runs if run.scenario_id == "burst"],
                             [True, False, False, True, False, False, True, False, False])
            self.assertEqual([run.candidate.id for run in plan.runs[:3]], [1, 2, 3])
            self.assertEqual(len({run.result_dir for run in plan.runs}), len(plan.runs))
            fixture = plan.runs[0].fixtures["maven"]
            self.assertTrue(fixture.output_dir.is_absolute())
            self.assertTrue(fixture.output_dir.is_relative_to(root / "_juice"))
            self.assertEqual(fixture.environment["FIXTURE_DIR"], str(fixture.output_dir))
            self.assertFalse(fixture.output_dir.exists())
            plan.runs[0].result_dir.mkdir(parents=True)
            repeated = build_test_plan(EXPERIMENT, COMPOSE, repository_root=root, results_dir=root / "results")
            self.assertEqual(
                [(run.scenario_id, run.run_id) for run in repeated.runs],
                [(run.scenario_id, run.run_id) for run in plan.runs],
            )

    def test_selects_one_run_with_stable_id_when_output_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            options = dict(repository_root=root, results_dir=root / "results", scenario_id="burst",
                           candidate_id=1, run_id="trial001")
            plan = build_test_plan(EXPERIMENT, COMPOSE, **options)
            self.assertEqual([(run.scenario_id, run.candidate.name, run.run_id) for run in plan.runs],
                             [("burst", "normal", "trial001")])
            plan.runs[0].result_dir.mkdir(parents=True)
            repeated = build_test_plan(EXPERIMENT, COMPOSE, **options)
            self.assertEqual(repeated.runs[0].result_dir, plan.runs[0].result_dir)


class TestCli(unittest.TestCase):
    def test_results_lists_only_matching_experiment_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp) / "_juice"
            results = workdir / "results"
            current = results / "burst" / "normal" / "run001"
            earlier = results / "moderate" / "staging" / "run002"
            other = results / "other" / "normal" / "run001"
            incomplete = results / "tmp" / "normal" / "run001"
            for directory in (current, earlier, other, incomplete):
                directory.mkdir(parents=True)
            (current / "run_metadata.json").write_text(json.dumps({"experiment": "maven-pull-through"}))
            (earlier / "run_metadata.json").write_text(json.dumps({"project": "pulp-juicer-maven-pull-through"}))
            (other / "run_metadata.json").write_text(json.dumps({
                "experiment": "another-experiment", "project": "pulp-juicer-maven-pull-through",
            }))

            command = ["results", "maven-pull-through", "--workdir", str(workdir)]
            response = CliRunner().invoke(cli, command)
            self.assertEqual(response.exit_code, 0, response.output)
            self.assertEqual(response.output.splitlines(), [str(current), str(earlier)])
            self.assertFalse((workdir / "plan-maven-pull-through.json").exists())

            unknown = CliRunner().invoke(cli, ["results", "unknown", "--workdir", str(workdir)])
            self.assertNotEqual(unknown.exit_code, 0)
            self.assertIn("Unknown experiment: unknown", unknown.output)

    def test_prepare_command_is_removed(self):
        result = CliRunner().invoke(cli, ["--help"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertNotIn("prepare", result.output)

    def test_ps_lists_only_pulp_juicer_compose_services(self):
        output = (
            "aaaaaaaaaaaa\tother-project\tdb\tother-db-1\tUp 2 hours\t5432/tcp\n"
            "bbbbbbbbbbbb\tpulp-juicer-maven-pull-through\tpulp\tpulp-juicer-maven-pull-through-pulp-1\tUp 1 minute (healthy)\t127.0.0.1:5001->80/tcp\n"
            "cccccccccccc\tpulp-juicer-maven-pull-through\tmaven_fixture\tpulp-juicer-maven-pull-through-maven_fixture-1\tExited (0)\t\n"
        )
        with patch("pulp_juicer.cli.subprocess.run", return_value=SimpleNamespace(stdout=output)) as docker:
            result = CliRunner().invoke(cli, ["ps"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("pulp-juicer-maven-pull-through", result.output)
        self.assertIn("maven_fixture", result.output)
        self.assertIn("Exited (0)", result.output)
        self.assertNotIn("other-project", result.output)
        self.assertIn("--all", docker.call_args.args[0])
        self.assertIn("label=com.docker.compose.project", docker.call_args.args[0])

    def test_ps_empty(self):
        with patch("pulp_juicer.cli.subprocess.run", return_value=SimpleNamespace(stdout="")):
            result = CliRunner().invoke(cli, ["ps"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("No pulp-juicer services found.", result.output)

    def test_clean_removes_only_pulp_juicer_containers_and_networks(self):
        calls = []

        def docker(command, **kwargs):
            calls.append(command)
            if command[1:3] == ["container", "ls"]:
                return SimpleNamespace(stdout=(
                    "aaaaaaaaaaaa\tother-project\tdb\tother-db-1\tUp\t\n"
                    "bbbbbbbbbbbb\tpulp-juicer-maven-pull-through\tpulp\tpulp-1\tUp\t\n"
                ))
            if command[1:3] == ["network", "ls"]:
                return SimpleNamespace(stdout=(
                    "cccccccccccc\tother-project\tother_default\n"
                    "dddddddddddd\tpulp-juicer-maven-pull-through\tpulp-juicer_default\n"
                ))
            return SimpleNamespace(stdout="")

        with patch("pulp_juicer.cli.subprocess.run", side_effect=docker):
            result = CliRunner().invoke(cli, ["clean"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Removed 1 container(s) and 1 network(s).", result.output)
        self.assertEqual(calls[2], ["docker", "container", "rm", "--force", "bbbbbbbbbbbb"])
        self.assertEqual(calls[3], ["docker", "network", "rm", "dddddddddddd"])

    def test_clean_empty_is_a_noop(self):
        with patch("pulp_juicer.cli.subprocess.run", return_value=SimpleNamespace(stdout="")) as docker:
            result = CliRunner().invoke(cli, ["clean"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Removed 0 container(s) and 0 network(s).", result.output)
        self.assertEqual(docker.call_count, 2)

    def test_ls_and_single_plan_scopes(self):
        runner = CliRunner()
        listed = runner.invoke(cli, ["ls"])
        self.assertEqual(listed.exit_code, 0, listed.output)
        self.assertIn("maven-pull-through: Compare Maven pull-through candidates under two load patterns.", listed.output)
        self.assertIn("  burst: Thirty concurrent users request 650 unique Maven artifacts.", listed.output)
        self.assertIn("  moderate: Ten concurrent users request 200 unique Maven artifacts.", listed.output)
        self.assertNotIn("response time and request completion", listed.output)
        self.assertNotIn("Runs:", listed.output)
        self.assertNotEqual(runner.invoke(cli, ["ls", "maven-pull-through"]).exit_code, 0)

        shown = runner.invoke(cli, ["show", "maven-pull-through"])
        self.assertEqual(shown.exit_code, 0, shown.output)
        self.assertNotIn("Compare Maven pull-through candidates under two load patterns.", shown.output)
        self.assertIn("response time and request completion", shown.output)
        self.assertIn("burst: Thirty concurrent users request 650 unique Maven artifacts. (9 runs, 30 users)", shown.output)
        self.assertIn("  1: normal\n", shown.output)
        self.assertNotIn("baseline", shown.output)
        run_rows = [line.split() for line in shown.output.split("Runs:\n", 1)[1].splitlines()]
        self.assertEqual(run_rows[0], ["SCENARIO", "RUN_ID", "CANDIDATE"])
        self.assertEqual(len(run_rows[1:]), TOTAL_RUNS)
        self.assertEqual(run_rows[1], ["burst", "run001", "normal"])
        self.assertEqual(run_rows[10], ["moderate", "run001", "normal"])
        self.assertIn(["moderate", "run006", "normal"], run_rows)
        unknown = runner.invoke(cli, ["show", "missing"])
        self.assertNotEqual(unknown.exit_code, 0)
        self.assertIn("Unknown experiment: missing", unknown.output)
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp) / "_juice"
            command = ["run", "maven-pull-through", "--workdir", str(workdir)]
            listed = runner.invoke(cli, command)
            self.assertEqual(listed.exit_code, 0, listed.output)
            self.assertIn(f"Prepared {TOTAL_RUNS} runs", listed.output)
            plan_file = workdir / "plan-maven-pull-through.json"
            self.assertEqual(list(workdir.glob("*.json")), [plan_file])
            self.assertEqual(len(load_plan_file(plan_file).runs), TOTAL_RUNS)
            self.assertEqual(len(load_plan_file(plan_file, scenario_id="burst").runs), 9)
            selected = load_plan_file(plan_file, scenario_id="burst", run_id="run001")
            self.assertEqual([(run.scenario_id, run.candidate.name, run.run_id) for run in selected.runs],
                             [("burst", "normal", "run001")])
            self.assertEqual(selected.results_dir, workdir / "results")
            self.assertEqual(selected.runs[0].fixture_builds[0].count, 1000)
            self.assertEqual(selected.runs[0].load.users, 30)
            edited = json.loads(plan_file.read_text())
            edited["scenarios"][0]["runs"][0]["load"]["users"] = 3
            edited["scenarios"][0]["runs"][0]["candidate"]["source"]["git"] = "../alternate-pulp-maven"
            plan_file.write_text(json.dumps(edited))
            selected = load_plan_file(plan_file, scenario_id="burst", run_id="run001")
            self.assertEqual(selected.runs[0].load.users, 3)
            self.assertEqual(selected.runs[0].candidate.source.git, "../alternate-pulp-maven")
            with self.assertRaisesRegex(ValueError, "requires a scenario"):
                load_plan_file(plan_file, run_id="run001")

    def test_run_loads_only_the_selected_unit_from_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp) / "_juice"
            listed = CliRunner().invoke(cli, ["run", "maven-pull-through", "--workdir", str(workdir)])
            self.assertEqual(listed.exit_code, 0, listed.output)
            result = SimpleNamespace(
                scenario_id="burst", run_id="run001", candidate_name="normal",
                completed_requests=2, expected_requests=2, failures=0,
                p95_response_ms=12.0, result_dir=Path(tmp), timed_out=False,
            )
            def report_progress(plan, **kwargs):
                kwargs["on_progress"]("Locust: 1/2 requests, 0 failures, 1 users, 1.0 req/s")
                return (result,)

            with patch("pulp_juicer.cli.execute_test_plan", side_effect=report_progress) as execute:
                response = CliRunner().invoke(cli, [
                    "run", "maven-pull-through", "--workdir", str(workdir),
                    "--scenario", "burst", "--run-id", "run001", "-f",
                ])
            self.assertEqual(response.exit_code, 0, response.output)
            plan = execute.call_args.args[0]
            self.assertEqual([(run.scenario_id, run.candidate.name, run.run_id) for run in plan.runs],
                             [("burst", "normal", "run001")])
            self.assertTrue(execute.call_args.kwargs["force"])
            self.assertIn(f"2/2 requests, 0 failures, p95=12.0 ms\nResults: {tmp}\n", response.output)
            self.assertIn("Locust: 1/2 requests", response.output)

    def test_cli_reports_skipped_unit(self):
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp) / "_juice"
            listed = CliRunner().invoke(cli, ["run", "maven-pull-through", "--workdir", str(workdir)])
            self.assertEqual(listed.exit_code, 0, listed.output)
            plan_file = workdir / "plan-maven-pull-through.json"
            load_plan_file(plan_file, scenario_id="burst", run_id="run001").runs[0].result_dir.mkdir(parents=True)
            with patch("pulp_juicer.execution.ComposeRuntime") as runtime_type:
                response = CliRunner().invoke(cli, [
                    "run", "maven-pull-through", "--workdir", str(workdir),
                    "--scenario", "burst", "--run-id", "run001",
                ])
            self.assertEqual(response.exit_code, 0, response.output)
            self.assertIn("Skipping burst/run001 normal", response.output)
            runtime_type.return_value.destroy_before_start.assert_not_called()

    def test_run_lists_available_scopes_and_all_selects_every_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp) / "_juice"
            command = ["run", "maven-pull-through", "--workdir", str(workdir)]
            listed = CliRunner().invoke(cli, command)
            self.assertEqual(listed.exit_code, 0, listed.output)
            self.assertIn(f"Prepared {TOTAL_RUNS} runs", listed.output)
            with patch("pulp_juicer.cli.execute_test_plan", return_value=()) as execute:
                listed_again = CliRunner().invoke(cli, command)
                self.assertEqual(listed_again.exit_code, 0, listed_again.output)
                self.assertNotIn("Prepared", listed_again.output)
                self.assertIn("burst:", listed_again.output)
                self.assertIn("moderate:", listed_again.output)
                self.assertIn("run001  normal", listed_again.output)
                execute.assert_not_called()

                whole = CliRunner().invoke(cli, [*command, "--all"])
                self.assertEqual(whole.exit_code, 0, whole.output)
                self.assertEqual(len(execute.call_args.args[0].runs), TOTAL_RUNS)

                scenario = CliRunner().invoke(cli, [*command, "--scenario", "burst"])
                self.assertEqual(scenario.exit_code, 0, scenario.output)
                self.assertEqual(len(execute.call_args.args[0].runs), 9)

            conflicting = CliRunner().invoke(cli, [*command, "--all", "--scenario", "burst"])
            self.assertNotEqual(conflicting.exit_code, 0)
            self.assertIn("cannot be combined", conflicting.output)

    def test_run_reuses_plan_until_refreshed(self):
        with tempfile.TemporaryDirectory() as tmp:
            command = ["run", "maven-pull-through", "--workdir", str(Path(tmp) / "_juice")]
            runner = CliRunner()
            first = runner.invoke(cli, command)
            self.assertEqual(first.exit_code, 0, first.output)
            plan_file = Path(tmp) / "_juice" / "plan-maven-pull-through.json"
            stored = json.loads(plan_file.read_text())
            stored["scenarios"][0]["runs"][0]["load"]["users"] = 3
            stored["runtime"]["pulp_connection"]["base_url"] = "http://127.0.0.1:9999"
            plan_file.write_text(json.dumps(stored))

            reused = runner.invoke(cli, command)
            self.assertEqual(reused.exit_code, 0, reused.output)
            self.assertNotIn("Prepared", reused.output)
            self.assertEqual(load_plan_file(plan_file).runs[0].load.users, 3)

            refreshed = runner.invoke(cli, [*command, "--refresh-plan"])
            self.assertEqual(refreshed.exit_code, 0, refreshed.output)
            plan = load_plan_file(plan_file)
            self.assertEqual(plan.runs[0].load.users, 30)
            self.assertEqual(plan.pulp_connection.base_url, "http://127.0.0.1:9999")


if __name__ == "__main__":
    unittest.main()
