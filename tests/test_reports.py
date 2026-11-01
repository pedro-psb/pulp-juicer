"""Each execution has its own report, rendered only once."""

import tempfile
import unittest
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pyarrow as pa
import pyarrow.parquet as pq
from click.testing import CliRunner

from pulp_juicer.cli import cli
from pulp_juicer.report import completed_runs, render_report
from pulp_juicer.tasking import TASK_SCHEMA
from pulp_juicer.trial_report import (
    _mean_sd,
    _median_curve,
    _task_gantt,
    render_trial_report,
)


class TestReportDiscovery(unittest.TestCase):
    def test_mean_sd_uses_runs_as_observations(self):
        self.assertEqual(_mean_sd([2, 4, 9]), "5.00\u00a0±\u00a03.61")
        self.assertEqual(_mean_sd([4]), "4.00\u00a0±\u00a0N/A")

    def test_median_curve_interpolates_without_extending_finished_runs(self):
        runs = [
            ("run001", [(0, 0), (2, 4)]),
            ("run002", [(0, 2), (1, 4), (3, 8)]),
        ]
        self.assertEqual(
            _median_curve(runs),
            [(0, 1), (1, 3), (2, 5), (3, 8)],
        )

    def test_task_gantt_uses_logged_duration_and_skips_partial_tasks(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "gantt.svg"
            start = datetime(2026, 1, 1, tzinfo=UTC)
            trial = SimpleNamespace(
                run_id="run001",
                metadata={"result": {"load_start_utc": start.isoformat()}},
                tasks=[
                    {
                        "task_id": "completed-id",
                        "task_type": "maven.pull.with.a.full.long.task.name",
                        "status": "completed",
                        "started_at_utc": start + timedelta(seconds=1),
                        "finished_at_utc": start + timedelta(seconds=3),
                    },
                    {
                        "task_id": "failed-id",
                        "task_type": "maven.pull",
                        "status": "failed",
                        "started_at_utc": start + timedelta(seconds=2),
                        "finished_at_utc": start + timedelta(seconds=4),
                    },
                    {
                        "task_id": "unfinished-id",
                        "task_type": "maven.pull",
                        "status": "started",
                        "started_at_utc": start,
                        "finished_at_utc": None,
                    },
                ],
            )
            self.assertTrue(_task_gantt(path, [trial]))
            root = ET.parse(path).getroot()
            bars = {
                bar.attrib["data-task-id"]: bar
                for bar in root.iter("{http://www.w3.org/2000/svg}rect")
                if "data-task-id" in bar.attrib
            }
            self.assertEqual(set(bars), {"completed-id", "failed-id"})
            self.assertEqual(bars["completed-id"].attrib["data-start-s"], "1.000")
            self.assertEqual(bars["completed-id"].attrib["data-duration-s"], "2.000")
            self.assertEqual(bars["failed-id"].attrib["fill"], "#dc2626")
            labels = [
                node.text for node in root.iter("{http://www.w3.org/2000/svg}text")
            ]
            self.assertIn("maven.pull.with.a.full.long.task.name", labels)
            self.assertEqual(labels.count("run001"), 1)
            self.assertNotIn("completed-id", labels)

    def test_old_generated_report_without_task_data_refreshes_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            execution = Path(tmp) / "smoke__001"
            run_dir = execution / "burst" / "normal" / "run001"
            run_dir.mkdir(parents=True)
            (run_dir / "locust_stats.csv").write_text(
                "Name,Request Count,Failure Count,Requests/s,Average Response Time,95%\n"
                "Aggregated,1,0,1.0,12,20\n"
            )
            (execution / "report.html").write_text("old report")
            (execution / "report.qmd").write_text("# Runs\n")

            def fake_quarto(command, **kwargs):
                (kwargs["cwd"] / "report.html").write_text("new report")

            with (
                patch("pulp_juicer.report.shutil.which", return_value="/bin/true"),
                patch(
                    "pulp_juicer.report.subprocess.run", side_effect=fake_quarto
                ) as rendered,
            ):
                self.assertEqual(render_report(execution), execution / "report.html")
            rendered.assert_called_once()
            source = (execution / "report.qmd").read_text()
            self.assertLess(source.index("# Summaries"), source.index("# Graphs"))
            self.assertLess(
                source.index("# Graphs"),
                source.index("# Run configuration and source files"),
            )

    def test_discovers_runs_within_one_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current = root / "burst" / "normal" / "run002"
            hidden = root / "_sources" / "normal" / "run003"
            for directory in (current, hidden):
                directory.mkdir(parents=True)
                (directory / "locust_stats.csv").write_text("Name\nAggregated\n")
            self.assertEqual(set(completed_runs(root)), {current})

    def test_report_command_handles_empty_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            results = Path(tmp) / "results"
            response = CliRunner().invoke(cli, ["report", "--results", str(results)])
        self.assertEqual(response.exit_code, 0, response.output)
        self.assertIn("No completed runs found", response.output)

    def test_report_command_renders_each_missing_execution_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            results = Path(tmp) / "results"
            first = results / "maven-pull-through__burst__run001__001"
            second = results / "maven-pull-through__burst__run001__002"
            existing = results / "maven-pull-through__burst__run001__003"
            for execution in (first, second):
                run_dir = execution / "burst" / "normal" / "run001"
                run_dir.mkdir(parents=True)
                (run_dir / "locust_stats.csv").write_text(
                    "Name,Request Count,Failure Count,Requests/s,Average Response Time,95%\n"
                    "Aggregated,2,1,1.5,12,20\n"
                )
            existing.mkdir()
            (existing / "report.html").write_text("<html>existing</html>")
            (existing / "report.qmd").write_text("existing source")
            existing_run = existing / "burst" / "normal" / "run001"
            existing_run.mkdir(parents=True)
            (existing_run / "locust_stats.csv").write_text(
                "Name,Request Count,Failure Count,Requests/s,Average Response Time,95%\n"
                "Aggregated,2,0,1.5,12,20\n"
            )

            def fake_quarto(command, **kwargs):
                self.assertIn(kwargs["cwd"], (first, second, existing))
                output = command[command.index("--output") + 1]
                (kwargs["cwd"] / output).write_text("<html>new</html>")

            with (
                patch("pulp_juicer.report.shutil.which", return_value="/bin/true"),
                patch(
                    "pulp_juicer.report.subprocess.run", side_effect=fake_quarto
                ) as render,
            ):
                response = CliRunner().invoke(
                    cli, ["report", "--results", str(results)]
                )

            self.assertEqual(response.exit_code, 0, response.output)
            self.assertEqual(
                response.output.splitlines(),
                [
                    "New Reports:",
                    str(first / "report.html"),
                    str(first / "trials.html"),
                    str(second / "report.html"),
                    str(second / "trials.html"),
                    str(existing / "trials.html"),
                    "",
                    "Existing:",
                    str(existing / "report.html"),
                ],
            )
            for execution in (first, second):
                document = (execution / "report.qmd").read_text()
                self.assertIn(execution.name, document)
                self.assertIn(
                    f"| {execution.name} | burst | run001 | normal | 2 | 1 |",
                    document,
                )
            self.assertEqual((existing / "report.qmd").read_text(), "existing source")
            self.assertEqual(
                (existing / "report.html").read_text(), "<html>existing</html>"
            )
            self.assertEqual(render.call_count, 5)

    def test_trial_report_compares_run_level_results_and_overlays(self):
        with tempfile.TemporaryDirectory() as tmp:
            execution = Path(tmp) / "maven-pull-through__burst__001"
            execution.mkdir()
            cases = (
                ("burst", "normal", "run001", 10, 100, 200, 50),
                ("burst", "staging", "run002", 15, 80, 150, 40),
                ("burst", "normal", "run003", 12, 120, 240, 60),
                ("burst", "staging", "run004", 17, 90, 170, 45),
                ("calibrate", "normal", "run001", 8, 130, 250, 55),
            )
            for scenario, candidate, run_id, rate, mean, p95, cpu in cases:
                run_dir = execution / scenario / candidate / run_id
                run_dir.mkdir(parents=True)
                (run_dir / "locust_stats.csv").write_text(
                    "Name,Request Count,Failure Count,Requests/s,Average Response Time,95%\n"
                    f"Aggregated,2,0,{rate},{mean},{p95}\n"
                )
                (run_dir / "run_metadata.json").write_text(
                    '{"load":{"users":2,"request_count":2},'
                    '"fixture":{"manifest_sha256":"same"},'
                    '"result":{"expected_requests":2,"completed_requests":2,'
                    '"load_start_utc":"2026-01-01T00:00:00+00:00"}}'
                )
                (run_dir / "requests.csv").write_text(
                    "started_at_utc,completed_at_utc\n"
                    "2026-01-01T00:00:00+00:00,2026-01-01T00:00:01+00:00\n"
                    "2026-01-01T00:00:01+00:00,2026-01-01T00:00:02+00:00\n"
                )
                (run_dir / "db_cpu.csv").write_text(
                    f"timestamp_utc,postgres_cpu_pct\n2026-01-01T00:00:01+00:00,{cpu}\n"
                )
                (run_dir / "db_cpu_summary.csv").write_text(
                    f"postgres_cpu_mean_pct\n{cpu}\n"
                )
                if scenario == "burst" and run_id in ("run001", "run002", "run003"):
                    start = datetime(2026, 1, 1, tzinfo=UTC)
                    pq.write_table(
                        pa.Table.from_pylist(
                            [
                                {
                                    "task_id": f"{run_id}-task",
                                    "task_type": "maven.pull",
                                    "status": "completed",
                                    "worker": "worker-1",
                                    "dispatched_at_utc": start
                                    - timedelta(
                                        milliseconds=100 if run_id == "run001" else 200
                                    ),
                                    "started_at_utc": start,
                                    "finished_at_utc": start + timedelta(seconds=1),
                                    "execution_time_us": 230000,
                                    "waiting_time_us": 100000
                                    if run_id == "run001"
                                    else 200000,
                                },
                                *(
                                    [
                                        {
                                            "task_id": f"{run_id}-extra",
                                            "task_type": "maven.pull",
                                            "status": "completed",
                                            "worker": "worker-1",
                                            "started_at_utc": start,
                                            "finished_at_utc": start
                                            + timedelta(seconds=1),
                                            "execution_time_us": 50000,
                                        }
                                    ]
                                    if run_id == "run002"
                                    else []
                                ),
                            ],
                            schema=TASK_SCHEMA,
                        ),
                        run_dir / "tasking.parquet",
                    )

            (execution / "execution.log").write_text(
                "2026-01-01T00:00:00Z INFO Starting burst/run001 normal\n"
                "2026-01-01T00:00:10Z INFO Pre-test hooks finished\n"
                "2026-01-01T00:00:12Z INFO Load test started\n"
                "2026-01-01T00:00:32Z INFO Load test stopped\n"
                "2026-01-01T00:00:35Z INFO Post-test hooks started\n"
                "2026-01-01T00:00:40Z INFO Finished burst/run001 normal: 2/2 requests\n"
                "2026-01-01T01:00:00Z INFO Starting burst/run003 normal\n"
                "2026-01-01T01:00:14Z INFO Pre-test hooks finished\n"
                "2026-01-01T01:00:16Z INFO Load test started\n"
                "2026-01-01T01:00:40Z INFO Load test stopped\n"
                "2026-01-01T01:00:42Z INFO Post-test hooks started\n"
                "2026-01-01T01:00:50Z INFO Finished burst/run003 normal: 2/2 requests\n"
            )

            def fake_quarto(command, **kwargs):
                (kwargs["cwd"] / "trials.html").write_text("<html>trials</html>")

            with (
                patch(
                    "pulp_juicer.trial_report.shutil.which", return_value="/bin/true"
                ),
                patch(
                    "pulp_juicer.trial_report.subprocess.run", side_effect=fake_quarto
                ) as quarto,
            ):
                report = render_trial_report(execution)

            self.assertEqual(report, execution / "trials.html")
            self.assertEqual(quarto.call_count, 1)
            source = (execution / "trials.qmd").read_text()
            self.assertIn(
                "| normal | 2 | 2.00\u00a0±\u00a00.00 | 0.00\u00a0±\u00a00.00 | 0.00\u00a0±\u00a00.00 | 11.00\u00a0±\u00a01.41 |",
                source,
            )
            self.assertIn(
                "| staging | 2 | 2.00\u00a0±\u00a00.00 | 0.00\u00a0±\u00a00.00 | 0.00\u00a0±\u00a00.00 | 16.00\u00a0±\u00a01.41 |",
                source,
            )
            self.assertIn("### Trial time by phase", source)
            self.assertIn(
                "| normal | 2 | 12.0\u00a0±\u00a02.8 | 2.0\u00a0±\u00a00.0 | 22.0\u00a0±\u00a02.8 | 2.5\u00a0±\u00a00.7 | 6.5\u00a0±\u00a02.1 | 45.0\u00a0±\u00a07.1 | 90.0 |",
                source,
            )
            self.assertIn("Total time across 2 timed trials:** 1.50 minutes", source)
            self.assertIn("Timing unavailable for 2 trial(s)", source)
            self.assertIn("| staging | 5.00 | -25.00 | -60.00 |", source)
            self.assertIn("### Pulp tasks", source)
            self.assertIn(
                "| Candidate | Trials | Tasks | Completed | Failed | Throughput (tasks/s) | Median execution (ms) | Total execution (ms) | Median wait (ms) |",
                source,
            )
            self.assertIn(
                "| normal | 2 | 1.00\u00a0±\u00a00.00 | 1.00\u00a0±\u00a00.00 | 0.00\u00a0±\u00a00.00 |",
                source,
            )
            self.assertIn(
                "| staging | 1 | 2.00\u00a0±\u00a0N/A | 2.00\u00a0±\u00a0N/A | 0.00\u00a0±\u00a0N/A |",
                source,
            )
            self.assertNotIn("Sample variance", source)
            self.assertNotIn("| normal | run001 |", source)
            self.assertIn("trials_burst_wait_boxplot.svg", source)
            self.assertGreater(
                source.index("trials_burst_wait_boxplot.svg"),
                source.index("# Graphs"),
            )
            wait_svg = execution / "trials_burst_wait_boxplot.svg"
            ET.parse(wait_svg)
            self.assertIn("normal (n=2)", wait_svg.read_text())
            self.assertIn("staging (n=1)", wait_svg.read_text())
            self.assertIn(
                "run001: median 100.00 ms from 1 task waits", wait_svg.read_text()
            )
            self.assertIn(
                "Median dispatch-to-start wait per run (ms)", wait_svg.read_text()
            )
            self.assertEqual(wait_svg.read_text().count("<circle"), 3)
            self.assertNotIn("<polyline", wait_svg.read_text())
            self.assertNotIn("No terminal record", source)
            frequent = source.split("#### Most frequent task types", 1)[1].split(
                "#### Total execution time by task type", 1
            )[0]
            self.assertIn("| normal | maven.pull | 2 | 2 | 0 | 460.00 |", frequent)
            self.assertIn("| Candidate | Task type | Total execution (ms) |", source)
            self.assertIn("| staging | maven.pull | 280.00 |", source)
            self.assertLess(
                source.index("#### Total execution time by task type"),
                source.index("# Graphs"),
            )
            self.assertLess(
                source.rfind("### Candidate summaries"), source.index("# Graphs")
            )
            self.assertLess(source.index("## calibrate"), source.index("# Graphs"))
            self.assertGreater(
                source.index("trials_burst_completed.svg"), source.index("# Graphs")
            )
            for metric in (
                "completed",
                "throughput",
                "starts",
                "cpu",
                "task_starts",
                "task_completions",
            ):
                svg = execution / f"trials_burst_{metric}.svg"
                self.assertTrue(svg.is_file())
                ET.parse(svg)
                self.assertIn("normal / run001", svg.read_text())
                self.assertIn(
                    "staging / run002"
                    if metric.startswith("task_")
                    else "staging / run004",
                    svg.read_text(),
                )
                self.assertIn('data-series="median"', svg.read_text())
                self.assertIn(
                    'stroke-opacity="0.22" data-series="run"', svg.read_text()
                )
            self.assertIn("Time since load start (s)", svg.read_text())
            for candidate, count in (("normal", 2), ("staging", 2)):
                gantt_name = f"trials_burst_{candidate}_task_gantt.svg"
                self.assertIn(gantt_name, source)
                self.assertGreater(source.index(gantt_name), source.index("# Graphs"))
                gantt = ET.parse(execution / gantt_name)
                bars = [
                    node
                    for node in gantt.iter("{http://www.w3.org/2000/svg}rect")
                    if "data-task-id" in node.attrib
                ]
                self.assertEqual(len(bars), count)
                if candidate == "normal":
                    self.assertEqual(
                        {bar.attrib["data-run"] for bar in bars},
                        {"run001", "run003"},
                    )
                    labels = [
                        node.text
                        for node in gantt.iter("{http://www.w3.org/2000/svg}text")
                    ]
                    self.assertEqual(labels.count("run001"), 1)
                    self.assertEqual(labels.count("run003"), 1)
            self.assertNotIn("trials_calibrate_normal_task_gantt.svg", source)
            self.assertTrue((execution / "trials_calibrate_completed.svg").is_file())
            with patch("pulp_juicer.trial_report.shutil.which") as which:
                self.assertEqual(render_trial_report(execution), report)
            which.assert_not_called()

            source_path = execution / "trials.qmd"
            source_path.write_text(
                source.replace("<!-- pulp-juicer-trial-layout: 21 -->", "")
            )
            with (
                patch(
                    "pulp_juicer.trial_report.shutil.which", return_value="/bin/true"
                ),
                patch(
                    "pulp_juicer.trial_report.subprocess.run", side_effect=fake_quarto
                ) as refreshed,
            ):
                self.assertEqual(render_trial_report(execution), report)
            refreshed.assert_called_once()
            self.assertIn(
                "<!-- pulp-juicer-trial-layout: 21 -->", source_path.read_text()
            )

    def test_existing_report_needs_no_quarto(self):
        with tempfile.TemporaryDirectory() as tmp:
            results = Path(tmp) / "results"
            execution = results / "maven-pull-through__001"
            execution.mkdir(parents=True)
            (execution / "report.html").write_text("existing")
            (execution / "trials.html").write_text("existing trials")
            with patch("pulp_juicer.report.shutil.which") as which:
                response = CliRunner().invoke(
                    cli, ["report", "--results", str(results)]
                )
            self.assertEqual(response.exit_code, 0, response.output)
            self.assertEqual(
                response.output.splitlines(),
                [
                    "Existing:",
                    str(execution / "report.html"),
                    str(execution / "trials.html"),
                ],
            )
            which.assert_not_called()


if __name__ == "__main__":
    unittest.main()
