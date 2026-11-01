"""Checks for plan expansion and the read-only CLI path."""

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from click.testing import CliRunner

from experiments.maven_pull_through.experiment import EXPERIMENT
from pulp_juicer.cli import cli
from pulp_juicer.plan import build_test_plan
from pulp_juicer.plan_file import load_plan_file, write_plan_file
from pulp_juicer.types import LoadStep, StepFunctionLoad, UnitaryImpulseLoad

COMPOSE = (
    Path(__file__).resolve().parents[1]
    / "src/experiments/maven_pull_through/compose.yaml"
)
TOTAL_RUNS = sum(len(scenario.run_order) for scenario in EXPERIMENT.scenarios)


class TestPlan(unittest.TestCase):
    def test_load_profiles_derive_locust_settings(self):
        impulse = UnitaryImpulseLoad(request_count=50)
        self.assertEqual((impulse.users, impulse.spawn_rate_per_second), (50, 50))
        self.assertEqual(impulse.max_duration, "5m")

        steps = StepFunctionLoad((LoadStep(0, 10), LoadStep(30, 40)))
        self.assertEqual(steps.users, 40)
        self.assertEqual(steps.spawn_rate(10, 40), 30)
        self.assertEqual(steps.max_duration, "5m")

    def test_step_profile_rejects_out_of_order_steps(self):
        with self.assertRaisesRegex(ValueError, "start at 0"):
            StepFunctionLoad((LoadStep(1, 2),), max_duration="5s")
        with self.assertRaisesRegex(ValueError, "must increase"):
            StepFunctionLoad((LoadStep(0, 2), LoadStep(0, 3)), max_duration="5s")

    def test_saved_plan_restores_both_load_profiles(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for scenario_id, profile_type in (
                ("calibrate", UnitaryImpulseLoad),
                ("burst", StepFunctionLoad),
            ):
                with self.subTest(scenario=scenario_id):
                    plan = build_test_plan(
                        EXPERIMENT,
                        COMPOSE,
                        repository_root=root,
                        execution_id=f"{scenario_id}__001",
                        scenario_id=scenario_id,
                    )
                    path = write_plan_file(plan)
                    saved = json.loads(path.read_text())
                    self.assertEqual(
                        saved["scenarios"][0]["runs"][0]["load"]["profile"],
                        profile_type.__name__,
                    )
                    restored = load_plan_file(path)
                    self.assertIsInstance(restored.runs[0].load, profile_type)
                    self.assertEqual(restored.runs[0].load, plan.runs[0].load)
                    for original, loaded in zip(plan.runs, restored.runs):
                        self.assertEqual(
                            loaded.candidate.pulp_settings,
                            original.candidate.pulp_settings,
                        )
                    self.assertEqual(
                        saved["scenarios"][0]["runs"][0]["candidate"]["pulp_settings"],
                        dict(plan.runs[0].candidate.pulp_settings),
                    )

    def test_expands_run_order_without_building_fixture(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = build_test_plan(
                EXPERIMENT,
                COMPOSE,
                repository_root=root,
                results_dir=root / "exeuctions",
            )
            self.assertEqual(len(plan.runs), TOTAL_RUNS)
            self.assertEqual(
                [run.candidate.id for run in plan.runs if run.scenario_id == "burst"],
                [1, 2, 2, 1],
            )
            self.assertEqual([run.candidate.id for run in plan.runs[:3]], [1, 2, 2])
            self.assertEqual(len({run.result_dir for run in plan.runs}), len(plan.runs))
            fixture = plan.runs[0].fixtures["maven"]
            self.assertTrue(fixture.output_dir.is_absolute())
            self.assertTrue(fixture.output_dir.is_relative_to(root / "_juice"))
            self.assertEqual(
                fixture.environment["FIXTURE_DIR"], str(fixture.output_dir)
            )
            self.assertFalse(fixture.output_dir.exists())
            plan.runs[0].result_dir.mkdir(parents=True)
            repeated = build_test_plan(
                EXPERIMENT,
                COMPOSE,
                repository_root=root,
                results_dir=root / "exeuctions",
            )
            self.assertEqual(
                [(run.scenario_id, run.run_id) for run in repeated.runs],
                [(run.scenario_id, run.run_id) for run in plan.runs],
            )

    def test_selects_one_run_with_stable_id_when_output_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            options = {
                "repository_root": root,
                "results_dir": root / "results",
                "scenario_id": "burst",
                "candidate_id": 1,
                "run_id": "trial001",
            }
            plan = build_test_plan(EXPERIMENT, COMPOSE, **options)
            self.assertEqual(
                [
                    (run.scenario_id, run.candidate.name, run.run_id)
                    for run in plan.runs
                ],
                [("burst", "normal", "trial001")],
            )
            plan.runs[0].result_dir.mkdir(parents=True)
            repeated = build_test_plan(EXPERIMENT, COMPOSE, **options)
            self.assertEqual(repeated.runs[0].result_dir, plan.runs[0].result_dir)

    def test_candidates_must_supply_every_required_source(self):
        candidate = EXPERIMENT.candidates[0]
        missing = replace(candidate, sources={"pulpcore": "../pulpcore"})
        with self.assertRaisesRegex(ValueError, "missing=\\['pulp_maven'\\]"):
            replace(EXPERIMENT, candidates=(missing,))

        extra = replace(
            candidate,
            sources={**candidate.sources, "pulp_python": "../pulp_python"},
        )
        with self.assertRaisesRegex(ValueError, "extra=\\['pulp_python'\\]"):
            replace(EXPERIMENT, candidates=(extra,))

        with self.assertRaisesRegex(ValueError, "Invalid candidate Pulp setting name"):
            replace(candidate, pulp_settings={"invalid-name": True})


class TestCli(unittest.TestCase):
    def test_results_lists_only_matching_experiment_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp) / "_juice"
            results = workdir / "exeuctions"
            current = (
                results
                / "maven-pull-through__burst__run001__001"
                / "burst"
                / "normal"
                / "run001"
            )
            earlier = (
                results
                / "maven-pull-through__moderate__002"
                / "moderate"
                / "staging"
                / "run002"
            )
            other = results / "another-experiment__003" / "other" / "normal" / "run001"
            incomplete = (
                results / "maven-pull-through__tmp__004" / "tmp" / "normal" / "run001"
            )
            for directory in (current, earlier, other, incomplete):
                directory.mkdir(parents=True)
            (current / "run_metadata.json").write_text(
                json.dumps({"experiment": "maven-pull-through"})
            )
            (earlier / "run_metadata.json").write_text(
                json.dumps({"project": "pulp-juicer-maven-pull-through"})
            )
            (other / "run_metadata.json").write_text(
                json.dumps(
                    {
                        "experiment": "another-experiment",
                        "project": "pulp-juicer-maven-pull-through",
                    }
                )
            )

            command = ["results", "maven-pull-through", "--workdir", str(workdir)]
            response = CliRunner().invoke(cli, command)
            self.assertEqual(response.exit_code, 0, response.output)
            self.assertEqual(response.output.splitlines(), [str(current), str(earlier)])

            current_report = current.parents[2]
            earlier_report = earlier.parents[2]
            (current_report / "report.qmd").write_text("report source")
            (current_report / "report.html").write_text("report output")
            (current_report / "trials.qmd").write_text("trial source")
            (current_report / "trials.html").write_text("trial output")
            (earlier_report / "report.html").write_text("earlier report")
            with_reports = CliRunner().invoke(cli, command)
            self.assertEqual(with_reports.exit_code, 0, with_reports.output)
            self.assertEqual(
                with_reports.output.splitlines(),
                [
                    str(current),
                    str(earlier),
                    str(current_report / "report.html"),
                    str(current_report / "report.qmd"),
                    str(current_report / "trials.html"),
                    str(current_report / "trials.qmd"),
                    str(earlier_report / "report.html"),
                ],
            )
            self.assertFalse((workdir / "plan-maven-pull-through.json").exists())

            unknown = CliRunner().invoke(
                cli, ["results", "unknown", "--workdir", str(workdir)]
            )
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
        with patch(
            "pulp_juicer.cli.subprocess.run",
            return_value=SimpleNamespace(stdout=output),
        ) as docker:
            result = CliRunner().invoke(cli, ["ps"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("pulp-juicer-maven-pull-through", result.output)
        self.assertIn("maven_fixture", result.output)
        self.assertIn("Exited (0)", result.output)
        self.assertNotIn("other-project", result.output)
        self.assertIn("--all", docker.call_args.args[0])
        self.assertIn("label=com.docker.compose.project", docker.call_args.args[0])

    def test_ps_empty(self):
        with patch(
            "pulp_juicer.cli.subprocess.run", return_value=SimpleNamespace(stdout="")
        ):
            result = CliRunner().invoke(cli, ["ps"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("No pulp-juicer services found.", result.output)

    def test_clean_removes_only_pulp_juicer_containers_and_networks(self):
        calls = []

        def docker(command, **kwargs):
            calls.append(command)
            if command[1:3] == ["container", "ls"]:
                return SimpleNamespace(
                    stdout=(
                        "aaaaaaaaaaaa\tother-project\tdb\tother-db-1\tUp\t\n"
                        "bbbbbbbbbbbb\tpulp-juicer-maven-pull-through\tpulp\tpulp-1\tUp\t\n"
                    )
                )
            if command[1:3] == ["network", "ls"]:
                return SimpleNamespace(
                    stdout=(
                        "cccccccccccc\tother-project\tother_default\n"
                        "dddddddddddd\tpulp-juicer-maven-pull-through\tpulp-juicer_default\n"
                    )
                )
            return SimpleNamespace(stdout="")

        with patch("pulp_juicer.cli.subprocess.run", side_effect=docker):
            result = CliRunner().invoke(cli, ["clean", "services"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Removed 1 container(s) and 1 network(s).", result.output)
        self.assertEqual(
            calls[2], ["docker", "container", "rm", "--force", "bbbbbbbbbbbb"]
        )
        self.assertEqual(calls[3], ["docker", "network", "rm", "dddddddddddd"])

    def test_clean_empty_is_a_noop(self):
        with patch(
            "pulp_juicer.cli.subprocess.run", return_value=SimpleNamespace(stdout="")
        ) as docker:
            result = CliRunner().invoke(cli, ["clean", "services"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Removed 0 container(s) and 0 network(s).", result.output)
        self.assertEqual(docker.call_count, 2)

    def test_clean_selected_data_keeps_plan_and_skips_docker(self):
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp) / "_juice"
            results = workdir / "exeuctions"
            fixtures = workdir / "fixture"
            results.mkdir(parents=True)
            fixtures.mkdir()
            (results / "run_metadata.json").write_text("result")
            (fixtures / "manifest.json").write_text("fixture")
            plan_file = workdir / "plan-maven-pull-through.json"
            plan_file.write_text("plan")
            with patch("pulp_juicer.cli.subprocess.run") as docker:
                response = CliRunner().invoke(
                    cli, ["clean", "results,fixtures", "--workdir", str(workdir)]
                )
            self.assertEqual(response.exit_code, 0, response.output)
            self.assertFalse(results.exists())
            self.assertFalse(fixtures.exists())
            self.assertTrue(plan_file.is_file())
            docker.assert_not_called()

    def test_clean_all_removes_data_and_services(self):
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp) / "_juice"
            (workdir / "exeuctions").mkdir(parents=True)
            (workdir / "fixture").mkdir()
            with patch(
                "pulp_juicer.cli.subprocess.run",
                return_value=SimpleNamespace(stdout=""),
            ) as docker:
                response = CliRunner().invoke(
                    cli, ["clean", "all", "--workdir", str(workdir)]
                )
            self.assertEqual(response.exit_code, 0, response.output)
            self.assertFalse((workdir / "exeuctions").exists())
            self.assertFalse((workdir / "fixture").exists())
            self.assertEqual(docker.call_count, 2)

    def test_clean_requires_valid_target(self):
        missing = CliRunner().invoke(cli, ["clean"])
        self.assertEqual(missing.exit_code, 2)
        self.assertIn(
            "Available targets: services, results, fixtures, all.", missing.output
        )
        self.assertIn("results,fixtures", missing.output)
        for arguments in (["unknown"], ["all,results"], ["results,"]):
            response = CliRunner().invoke(cli, ["clean", *arguments])
            self.assertEqual(response.exit_code, 2)
            self.assertIn("Usage:", response.output)

    def test_ls_and_single_plan_scopes(self):
        runner = CliRunner()
        listed = runner.invoke(cli, ["ls"])
        self.assertEqual(listed.exit_code, 0, listed.output)
        run_without_experiment = runner.invoke(cli, ["squeeze"])
        self.assertEqual(run_without_experiment.exit_code, 2)
        self.assertIn(
            "maven-pull-through: Compare Maven pull-through and upload candidates under load.",
            run_without_experiment.output,
        )
        self.assertNotIn("  burst:", run_without_experiment.output)
        self.assertNotIn("  calibrate:", run_without_experiment.output)
        self.assertNotIn("run001", run_without_experiment.output)
        self.assertIn("Available experiments:\n", run_without_experiment.output)
        self.assertIn(
            "Use pulp-juicer squeeze EXPERIMENT to list available runs.",
            run_without_experiment.output,
        )
        self.assertNotIn("Usage:", run_without_experiment.output)
        self.assertIn(
            "maven-pull-through: Compare Maven pull-through and upload candidates under load.",
            listed.output,
        )
        self.assertIn(
            "  burst: Thirty concurrent users request 650 unique Maven artifacts.",
            listed.output,
        )
        self.assertIn(
            "  calibrate: Analyse system response to a big impulse load",
            listed.output,
        )
        self.assertNotIn("response time and request completion", listed.output)
        self.assertNotIn("Runs:", listed.output)
        self.assertNotEqual(
            runner.invoke(cli, ["ls", "maven-pull-through"]).exit_code, 0
        )

        shown_without_experiment = runner.invoke(cli, ["show"])
        self.assertEqual(
            shown_without_experiment.exit_code, 0, shown_without_experiment.output
        )
        self.assertIn("Available experiments:\n", shown_without_experiment.output)
        self.assertIn(
            "maven-pull-through: Compare Maven pull-through and upload candidates under load.",
            shown_without_experiment.output,
        )
        self.assertIn(
            "Use pulp-juicer show EXPERIMENT for details.",
            shown_without_experiment.output,
        )
        self.assertNotIn("  burst:", shown_without_experiment.output)
        self.assertNotIn("Required sources:", shown_without_experiment.output)

        shown = runner.invoke(cli, ["show", "maven-pull-through"])
        self.assertEqual(shown.exit_code, 0, shown.output)
        self.assertNotIn(
            "Compare Maven pull-through and upload candidates under load.",
            shown.output,
        )
        self.assertIn("response time and request completion", shown.output)
        self.assertIn("Required sources: pulpcore, pulp_maven", shown.output)
        self.assertIn(
            "burst: Thirty concurrent users request 650 unique Maven artifacts. (4 runs, 30 users)",
            shown.output,
        )
        self.assertIn(
            "1: normal — Released Pulp sources with standard Maven repository behavior.",
            shown.output,
        )
        self.assertIn(
            f"2: staging — {EXPERIMENT.candidates[1].description}",
            shown.output,
        )
        self.assertNotIn("baseline", shown.output)
        run_rows = [
            line.split() for line in shown.output.split("Runs:\n", 1)[1].splitlines()
        ]
        self.assertEqual(run_rows[0], ["SCENARIO", "RUN_ID", "CANDIDATE"])
        self.assertEqual(len(run_rows[1:]), TOTAL_RUNS)
        self.assertEqual(run_rows[1], ["calibrate", "run001", "normal"])
        expected_rows = [
            [run.scenario_id, run.run_id, run.candidate.name]
            for run in build_test_plan(EXPERIMENT, COMPOSE).runs
        ]
        self.assertEqual(run_rows[1:], expected_rows)
        unknown = runner.invoke(cli, ["show", "missing"])
        self.assertNotEqual(unknown.exit_code, 0)
        self.assertIn("Unknown experiment: missing", unknown.output)
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp) / "_juice"
            command = ["squeeze", "maven-pull-through", "--workdir", str(workdir)]
            listed = runner.invoke(cli, command)
            self.assertEqual(listed.exit_code, 0, listed.output)
            self.assertIn("Available runs for maven-pull-through", listed.output)
            self.assertFalse(workdir.exists())

            recorded = []
            with patch(
                "pulp_juicer.cli.execute_test_plan",
                side_effect=lambda plan, **kwargs: recorded.append(plan) or (),
            ):
                selected = runner.invoke(cli, [*command, "--scenario", "burst"])
            self.assertEqual(selected.exit_code, 0, selected.output)
            self.assertEqual(len(recorded[0].runs), 4)
            self.assertEqual(recorded[0].execution_id, "maven-pull-through__burst__001")
            self.assertEqual(list(workdir.glob("plan-*.json")), [])
            self.assertTrue(recorded[0].results_dir.is_dir())

            with patch("pulp_juicer.cli.execute_test_plan", return_value=()):
                saved = runner.invoke(
                    cli, [*command, "--scenario", "burst", "--save-plan"]
                )
            self.assertEqual(saved.exit_code, 0, saved.output)
            plan_file = workdir / "plan-maven-pull-through__burst__002.json"
            self.assertIn(f"Plan: {plan_file}", saved.output)
            self.assertEqual(len(load_plan_file(plan_file).runs), 4)
            data = json.loads(plan_file.read_text())
            self.assertNotIn("password", data["runtime"]["pulp_connection"])
            self.assertEqual(data["execution_id"], "maven-pull-through__burst__002")
            saved_candidate = data["scenarios"][0]["runs"][0]["candidate"]
            self.assertEqual(
                saved_candidate["description"],
                "Released Pulp sources with standard Maven repository behavior.",
            )
            self.assertEqual(
                saved_candidate["pre_test_functions"],
                ["Create Maven remote, repository, and distribution"],
            )
            loaded = load_plan_file(plan_file)
            self.assertTrue(
                callable(loaded.runs[0].candidate.pre_test_functions[0].setup_fn)
            )

    def test_squeeze_executes_each_scope_from_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp) / "_juice"
            command = ["squeeze", "maven-pull-through", "--workdir", str(workdir)]
            recorded = []

            def fake_execute(plan, **kwargs):
                recorded.append(plan)
                return ()

            with patch("pulp_juicer.cli.execute_test_plan", side_effect=fake_execute):
                for scope in (
                    ["--all"],
                    ["--scenario", "burst"],
                    ["--scenario", "burst", "--run-id", "run002"],
                ):
                    response = CliRunner().invoke(cli, [*command, *scope])
                    self.assertEqual(response.exit_code, 0, response.output)

            self.assertEqual(
                [plan.execution_id for plan in recorded],
                [
                    "maven-pull-through__001",
                    "maven-pull-through__burst__002",
                    "maven-pull-through__burst__run002__003",
                ],
            )
            self.assertEqual([len(plan.runs) for plan in recorded], [TOTAL_RUNS, 4, 1])
            self.assertEqual(recorded[-1].runs[0].run_id, "run002")
            for plan in recorded:
                self.assertEqual(
                    plan.results_dir, workdir / "exeuctions" / plan.execution_id
                )
                self.assertTrue(plan.results_dir.is_dir())
            self.assertEqual(list(workdir.glob("plan-*.json")), [])

    def test_repeated_squeeze_gets_new_id_and_keeps_existing_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp) / "_juice"
            command = [
                "squeeze",
                "maven-pull-through",
                "--workdir",
                str(workdir),
                "--scenario",
                "burst",
                "--run-id",
                "run001",
            ]
            with patch("pulp_juicer.cli.execute_test_plan", return_value=()) as execute:
                first = CliRunner().invoke(cli, command)
            self.assertEqual(first.exit_code, 0, first.output)
            first_plan = execute.call_args.args[0]
            first_result = first_plan.runs[0].result_dir
            first_result.mkdir(parents=True)
            (first_result / "keep.txt").write_text("keep")

            with patch("pulp_juicer.cli.execute_test_plan", return_value=()) as execute:
                repeated = CliRunner().invoke(cli, command)
            self.assertEqual(repeated.exit_code, 0, repeated.output)
            self.assertEqual(
                execute.call_args.args[0].execution_id,
                "maven-pull-through__burst__run001__002",
            )
            self.assertTrue((first_result / "keep.txt").is_file())
            self.assertEqual(list(workdir.glob("plan-*.json")), [])

            cleaned = CliRunner().invoke(
                cli, ["clean", "results", "--workdir", str(workdir)]
            )
            self.assertEqual(cleaned.exit_code, 0, cleaned.output)
            with patch("pulp_juicer.cli.execute_test_plan", return_value=()) as execute:
                after_clean = CliRunner().invoke(cli, command)
            self.assertEqual(after_clean.exit_code, 0, after_clean.output)
            self.assertEqual(
                execute.call_args.args[0].execution_id,
                "maven-pull-through__burst__run001__003",
            )

    def test_force_replaces_latest_matching_execution_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp) / "_juice"
            command = [
                "squeeze",
                "maven-pull-through",
                "--workdir",
                str(workdir),
                "--scenario",
                "burst",
                "--run-id",
                "run001",
            ]
            with patch("pulp_juicer.cli.execute_test_plan", return_value=()):
                for _ in range(2):
                    created = CliRunner().invoke(cli, command)
                    self.assertEqual(created.exit_code, 0, created.output)
            first_root = (
                workdir / "exeuctions" / "maven-pull-through__burst__run001__001"
            )
            (first_root / "keep.txt").write_text("keep")
            latest_id = "maven-pull-through__burst__run001__002"
            result_root = workdir / "exeuctions" / latest_id
            (result_root / "old.txt").write_text("old")
            other_result = (
                workdir / "exeuctions" / "maven-pull-through__burst__003" / "keep.txt"
            )
            other_result.parent.mkdir(parents=True)
            other_result.write_text("keep")

            def check_replacement(plan, **kwargs):
                self.assertEqual(plan.execution_id, latest_id)
                self.assertTrue(result_root.is_dir())
                self.assertFalse((result_root / "old.txt").exists())
                return ()

            with patch(
                "pulp_juicer.cli.execute_test_plan", side_effect=check_replacement
            ):
                replaced = CliRunner().invoke(cli, [*command, "-f"])
            self.assertEqual(replaced.exit_code, 0, replaced.output)
            self.assertTrue((first_root / "keep.txt").is_file())
            self.assertTrue(other_result.is_file())
            self.assertEqual(list(workdir.glob("plan-*.json")), [])

    def test_force_without_save_plan_removes_previous_saved_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp) / "_juice"
            command = [
                "squeeze",
                "maven-pull-through",
                "--workdir",
                str(workdir),
                "--scenario",
                "burst",
                "--run-id",
                "run001",
            ]
            with patch("pulp_juicer.cli.execute_test_plan", return_value=()):
                saved = CliRunner().invoke(cli, [*command, "--save-plan"])
                self.assertEqual(saved.exit_code, 0, saved.output)
                replaced = CliRunner().invoke(cli, [*command, "-f"])
                self.assertEqual(replaced.exit_code, 0, replaced.output)
            self.assertEqual(list(workdir.glob("plan-*.json")), [])
            self.assertIn(
                "Execution: maven-pull-through__burst__run001__001", replaced.output
            )

    def test_failed_squeeze_can_be_replaced_without_a_saved_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp) / "_juice"
            command = [
                "squeeze",
                "maven-pull-through",
                "--workdir",
                str(workdir),
                "--scenario",
                "burst",
                "--run-id",
                "run001",
            ]

            def fail_with_compose_log(plan, **kwargs):
                compose_log = plan.runs[0].result_dir / "compose.log"
                compose_log.parent.mkdir(parents=True)
                compose_log.write_text("pulp | startup error\n")
                raise RuntimeError("failed")

            with patch(
                "pulp_juicer.cli.execute_test_plan", side_effect=fail_with_compose_log
            ):
                failed = CliRunner().invoke(cli, command)
            self.assertNotEqual(failed.exit_code, 0)
            execution_id = "maven-pull-through__burst__run001__001"
            self.assertTrue((workdir / "exeuctions" / execution_id).is_dir())
            compose_log = (
                workdir
                / "exeuctions"
                / execution_id
                / "burst"
                / "normal"
                / "run001"
                / "compose.log"
            )
            self.assertIn(f"Compose log: {compose_log}", failed.output)
            self.assertEqual(list(workdir.glob("plan-*.json")), [])
            log_text = (
                workdir / "exeuctions" / execution_id / "execution.log"
            ).read_text()
            self.assertIn("Execution failed", log_text)
            self.assertIn("RuntimeError: failed", log_text)

            with patch("pulp_juicer.cli.execute_test_plan", return_value=()) as execute:
                replaced = CliRunner().invoke(cli, [*command, "-f"])
            self.assertEqual(replaced.exit_code, 0, replaced.output)
            self.assertEqual(execute.call_args.args[0].execution_id, execution_id)

    def test_squeeze_reports_request_failures_without_failing_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp) / "_juice"
            command = [
                "squeeze",
                "maven-pull-through",
                "--workdir",
                str(workdir),
                "--scenario",
                "burst",
                "--run-id",
                "run001",
            ]

            def failed_requests(plan, **kwargs):
                run = plan.runs[0]
                run.result_dir.mkdir(parents=True)
                (run.result_dir / "compose.log").write_text("pulp | request error\n")
                return (
                    SimpleNamespace(
                        scenario_id=run.scenario_id,
                        run_id=run.run_id,
                        candidate_name=run.candidate.name,
                        completed_requests=10,
                        expected_requests=10,
                        failures=2,
                        p95_response_ms=15,
                        result_dir=run.result_dir,
                        timed_out=False,
                    ),
                )

            with patch(
                "pulp_juicer.cli.execute_test_plan", side_effect=failed_requests
            ):
                response = CliRunner().invoke(cli, command)
            self.assertEqual(response.exit_code, 0, response.output)
            self.assertIn("10/10 requests, 2 failures", response.output)
            self.assertIn(
                f"Compose log: {workdir / 'exeuctions' / 'maven-pull-through__burst__run001__001' / 'burst' / 'normal' / 'run001' / 'compose.log'}",
                response.output,
            )
            self.assertIn("Results:", response.output)

    def test_incomplete_squeeze_prints_compose_log_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp) / "_juice"

            def incomplete_run(plan, **kwargs):
                run = plan.runs[0]
                run.result_dir.mkdir(parents=True)
                (run.result_dir / "compose.log").write_text("pulp | stopped\n")
                return (
                    SimpleNamespace(
                        scenario_id=run.scenario_id,
                        run_id=run.run_id,
                        candidate_name=run.candidate.name,
                        completed_requests=1,
                        expected_requests=10,
                        failures=0,
                        p95_response_ms=15,
                        result_dir=run.result_dir,
                        timed_out=True,
                    ),
                )

            with patch("pulp_juicer.cli.execute_test_plan", side_effect=incomplete_run):
                response = CliRunner().invoke(
                    cli,
                    [
                        "squeeze",
                        "maven-pull-through",
                        "--workdir",
                        str(workdir),
                        "--scenario",
                        "burst",
                        "--run-id",
                        "run001",
                    ],
                )

            self.assertNotEqual(response.exit_code, 0)
            compose_log = (
                workdir
                / "exeuctions"
                / "maven-pull-through__burst__run001__001"
                / "burst"
                / "normal"
                / "run001"
                / "compose.log"
            )
            self.assertIn(f"Compose log: {compose_log}", response.output)
            self.assertIn("One or more runs were incomplete", response.output)

    def test_execution_log_records_plan_and_load_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp) / "_juice"
            command = [
                "squeeze",
                "maven-pull-through",
                "--workdir",
                str(workdir),
                "--scenario",
                "burst",
                "--run-id",
                "run001",
            ]

            def fake_execute(plan, **kwargs):
                kwargs["on_progress"]("Pre-test hooks started")
                kwargs["on_progress"]("Load test started")
                kwargs["on_progress"]("Load test stopped")
                kwargs["on_progress"]("Post-test hooks finished")
                return ()

            with patch("pulp_juicer.cli.execute_test_plan", side_effect=fake_execute):
                response = CliRunner().invoke(cli, command)
            self.assertEqual(response.exit_code, 0, response.output)
            log_path = (
                workdir
                / "exeuctions"
                / "maven-pull-through__burst__run001__001"
                / "execution.log"
            )
            self.assertIn(f"Log: {log_path}", response.output)
            lines = log_path.read_text().splitlines()
            self.assertTrue(lines[0].endswith("INFO Plan preparation started"))
            self.assertIn("Plan preparation finished: 1 runs", lines[1])
            self.assertIn("Pre-test hooks started", log_path.read_text())
            self.assertIn("Load test started", log_path.read_text())
            self.assertIn("Load test stopped", log_path.read_text())
            self.assertIn("Post-test hooks finished", log_path.read_text())

    def test_squeeze_rejects_invalid_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            command = [
                "squeeze",
                "maven-pull-through",
                "--workdir",
                str(Path(tmp) / "_juice"),
            ]
            for scope in (
                ["--run-id", "run001"],
                ["--all", "--scenario", "burst"],
                ["--scenario", "burst", "--run-id", "missing"],
            ):
                response = CliRunner().invoke(cli, [*command, *scope])
                self.assertNotEqual(response.exit_code, 0)
            self.assertEqual(list((Path(tmp) / "_juice").glob("plan-*.json")), [])

    def test_post_debug_requires_single_run_and_prints_inspection_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp) / "_juice"
            command = [
                "squeeze",
                "maven-pull-through",
                "--workdir",
                str(workdir),
                "--post-debug",
            ]
            for scope in ([], ["--all"], ["--scenario", "burst"]):
                response = CliRunner().invoke(cli, [*command, *scope])
                self.assertNotEqual(response.exit_code, 0)
                self.assertIn(
                    "--post-debug requires --scenario and --run-id", response.output
                )
            self.assertFalse(workdir.exists())

            with patch("pulp_juicer.cli.execute_test_plan", return_value=()) as execute:
                response = CliRunner().invoke(
                    cli,
                    [*command, "--scenario", "burst", "--run-id", "run001"],
                    env={"COMPOSE": "docker compose"},
                )
            self.assertEqual(response.exit_code, 0, response.output)
            self.assertTrue(execute.call_args.kwargs["post_debug"])
            self.assertEqual(len(execute.call_args.args[0].runs), 1)
            self.assertIn("Post-debug: services are still running", response.output)
            self.assertIn(
                "export COMPOSE_PROJECT_NAME=pulp-juicer-maven-pull-through",
                response.output,
            )
            self.assertIn(f"export COMPOSE_FILE={COMPOSE}", response.output)
            self.assertIn("docker compose ps", response.output)
            self.assertIn("docker compose exec pulp sh", response.output)
            self.assertIn("docker compose logs --follow pulp", response.output)
            self.assertIn(
                "curl --user admin http://127.0.0.1:5001/pulp/api/v3/status/",
                response.output,
            )
            self.assertIn("docker compose down --remove-orphans", response.output)
            self.assertNotIn("--project-name", response.output)
            self.assertNotIn("--file", response.output)

            with patch(
                "pulp_juicer.cli.execute_test_plan",
                side_effect=RuntimeError("load failed"),
            ):
                failed = CliRunner().invoke(
                    cli,
                    [*command, "--scenario", "burst", "--run-id", "run001"],
                    env={"COMPOSE": "docker compose"},
                )
            self.assertNotEqual(failed.exit_code, 0)
            self.assertIn(
                "Post-debug commands (services may be running)", failed.output
            )
            self.assertIn("exec pulp sh", failed.output)


if __name__ == "__main__":
    unittest.main()
