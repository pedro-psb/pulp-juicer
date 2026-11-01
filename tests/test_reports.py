"""Each execution has its own report, rendered only once."""

import itertools
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pyarrow as pa
import pyarrow.parquet as pq
from click.testing import CliRunner

from pulp_juicer.cli import cli
from pulp_juicer.collector.task_db import TASK_SCHEMA
from pulp_juicer.report import completed_runs, prepare_run, render_report
from pulp_juicer.trial_report import (
    _mean_sd,
    _median_curve,
    _smoothed_mean_rate,
    _task_gantt,
    render_trial_report,
)


def write_postgres_samples(run_dir, cpu):
    output = run_dir / "collectors"
    output.mkdir(exist_ok=True)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    rows = [
        {
            "timestamp_utc": start + timedelta(seconds=second),
            "interval_s": 1.0,
            "cpu_pct": float(cpu),
            "cpu_seconds": float(cpu) / 100,
            "pss_bytes": 64 * 2**20,
            "processes": 5,
        }
        for second in (0, 1)
    ]
    pq.write_table(pa.Table.from_pylist(rows), output / "postgres.parquet")


def write_content_samples(run_dir):
    output = run_dir / "collectors"
    output.mkdir(exist_ok=True)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    io_rows = [
        {
            "timestamp_utc": start + timedelta(seconds=second),
            "worker_pid": pid,
            "interval_s": 1.0,
            "storage_read_bytes_per_s": rate * 2**20,
            "storage_write_bytes_per_s": 0.5 * 2**20,
            "read_syscalls_per_s": 10.0,
            "write_syscalls_per_s": 5.0,
        }
        for second in (0, 1)
        for pid, rate in ((11, 1), (12, 2))
    ]
    pq.write_table(pa.Table.from_pylist(io_rows), output / "content_io.parquet")
    temp_rows = [
        {
            "timestamp_utc": start + timedelta(seconds=second),
            "active_files": files,
            "active_bytes": size * 2**20,
            "allocated_bytes": size * 2**20,
            "root_files": 1,
            "root_bytes": 2**20,
        }
        for second, files, size in ((0, 2, 1), (1, 4, 3))
    ]
    pq.write_table(pa.Table.from_pylist(temp_rows), output / "pulp_temp.parquet")


def write_api_samples(run_dir):
    output = run_dir / "collectors"
    output.mkdir(exist_ok=True)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    rows = [
        {
            "timestamp_utc": start + timedelta(seconds=second),
            "worker_pid": pid,
            "interval_s": 1.0,
            "cpu_pct": float(cpu),
            "cpu_seconds": cpu / 100,
            "pss_bytes": 40 * 2**20,
            "storage_read_bytes_per_s": 2**20,
            "storage_write_bytes_per_s": 0.5 * 2**20,
            "read_syscalls_per_s": 10.0,
            "write_syscalls_per_s": 5.0,
        }
        for second in (0, 1)
        for pid, cpu in ((21, 10), (22, 20))
    ]
    pq.write_table(pa.Table.from_pylist(rows), output / "api_worker.parquet")


class TestReportDiscovery(unittest.TestCase):
    def test_individual_timelines_draw_new_requests_as_lines_on_all_charts(self):
        with tempfile.TemporaryDirectory() as tmp:
            execution = Path(tmp)
            run_dir = execution / "burst" / "normal" / "run001"
            run_dir.mkdir(parents=True)
            (run_dir / "locust_stats.csv").write_text(
                "Name,Request Count,Failure Count,Requests/s,Average Response Time,50%,95%,99%\n"
                "Aggregated,2,0,1,10,9,12,15\n"
            )
            (run_dir / "locust_stats_history.csv").write_text(
                "Name,Timestamp,Total Request Count,User Count\n"
                "Aggregated,1767225600,0,2\n"
                "Aggregated,1767225601,2,2\n"
            )
            (run_dir / "requests.csv").write_text(
                "started_at_utc\n2026-01-01T00:00:00+00:00\n2026-01-01T00:00:01+00:00\n"
            )
            write_postgres_samples(run_dir, 35)
            write_content_samples(run_dir)
            write_api_samples(run_dir)

            _, summary, graphs, details = prepare_run(run_dir, execution)
            self.assertTrue((run_dir / "graphs" / "report_db_memory.svg").is_file())
            self.assertTrue((run_dir / "locust_stats.parquet").is_file())
            self.assertTrue((run_dir / "raw" / "locust_stats.csv").is_file())
            self.assertTrue((run_dir / "requests.parquet").is_file())
            self.assertIn("Mean PSS", "\n".join(summary))
            self.assertIn("| Storage read | 6.00 MiB |", "\n".join(summary))
            self.assertIn("#### API-worker resources", "\n".join(summary))
            self.assertIn("| Mean CPU | 30.00% |", "\n".join(summary))
            self.assertIn("#### API-worker I/O", "\n".join(summary))
            self.assertIn("| Peak active files | 4 |", "\n".join(summary))
            self.assertIn("report_content_read_mib_s.svg", "\n".join(graphs))
            self.assertIn("report_api_cpu.svg", "\n".join(graphs))
            self.assertIn("report_api_read_mib_s.svg", "\n".join(graphs))
            self.assertIn("report_temp_active_bytes.svg", "\n".join(graphs))
            self.assertIn("content_io collector Parquet", "\n".join(details))
            self.assertIn("api_worker collector Parquet", "\n".join(details))
            for name in ("read_mib_s", "write_mib_s"):
                svg = (run_dir / "graphs" / f"report_content_{name}.svg").read_text()
                self.assertIn("Completed requests (right)", svg)
                self.assertIn("New requests/s (right)", svg)
                self.assertIn('stroke="#64748b"', svg)
                self.assertIn('stroke="#7c3aed"', svg)
            for name in ("completed", "throughput", "db_cpu", "api_cpu"):
                with self.subTest(chart=name):
                    root = ET.parse(run_dir / "graphs" / f"report_{name}.svg").getroot()
                    purple_lines = [
                        node
                        for node in root.iter("{http://www.w3.org/2000/svg}polyline")
                        if node.attrib.get("stroke") == "#7c3aed"
                    ]
                    self.assertEqual(len(purple_lines), 1)
                    self.assertNotIn(
                        "<rect x=",
                        (run_dir / "graphs" / f"report_{name}.svg").read_text(),
                    )

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

    def test_smoothed_mean_rate_preserves_mean_completed_requests(self):
        runs = [
            ("run001", [(0, 4), (1, 0), (2, 0)]),
            ("run002", [(0, 0), (1, 0), (2, 2)]),
        ]
        curve = _smoothed_mean_rate(runs)
        area = sum(
            (a[1] + b[1]) * (b[0] - a[0]) / 2 for a, b in itertools.pairwise(curve)
        )
        self.assertAlmostEqual(area, 3)
        self.assertGreater(curve[1][1], 0)
        self.assertTrue(all(value >= 0 for _, value in curve))

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

    def test_report_without_id_lists_available_executions(self):
        with tempfile.TemporaryDirectory() as tmp:
            results = Path(tmp) / "results"
            response = CliRunner().invoke(cli, ["report", "--results", str(results)])
            self.assertEqual(response.exit_code, 0, response.output)
            self.assertIn("No executions found", response.output)
            (results / "maven-pull-through__002").mkdir(parents=True)
            (results / "maven-pull-through__001").mkdir()
            (results / "unrelated").mkdir()
            with patch("pulp_juicer.cli.render_report") as render:
                listed = CliRunner().invoke(cli, ["report", "--results", str(results)])
            self.assertEqual(listed.exit_code, 0, listed.output)
            self.assertEqual(
                listed.output.splitlines(),
                [
                    "Available execution IDs:",
                    "  maven-pull-through__001",
                    "  maven-pull-through__002",
                    "Use pulp-juicer report EXECUTION_ID to generate its reports.",
                ],
            )
            render.assert_not_called()

    def test_report_zip_contains_reports_and_source_data_and_reuses_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            results = Path(tmp) / "results"
            execution = results / "maven-pull-through__001"
            run_dir = execution / "upload-impulse" / "normal" / "run001"
            collectors = run_dir / "collectors"
            collectors.mkdir(parents=True)
            (execution / "report.html").write_text("detailed report")
            (execution / "trials.html").write_text("trial report")
            (execution / "execution.log").write_text("run log")
            (run_dir / "requests.csv").write_text("request data")
            (collectors / "api_worker.parquet").write_bytes(b"parquet data")
            args = ["report", execution.name, "--results", str(results), "--zip"]
            with (
                patch(
                    "pulp_juicer.cli.render_report",
                    return_value=execution / "report.html",
                ),
                patch(
                    "pulp_juicer.cli.render_trial_report",
                    return_value=execution / "trials.html",
                ),
            ):
                first = CliRunner().invoke(cli, args)
                self.assertEqual(first.exit_code, 0, first.output)
                archive = results / f"{execution.name}.zip"
                self.assertIn("New Archives:\n" + str(archive), first.output)
                with zipfile.ZipFile(archive) as bundle:
                    self.assertEqual(
                        set(bundle.namelist()),
                        {
                            f"{execution.name}/report.html",
                            f"{execution.name}/trials.html",
                            f"{execution.name}/execution.log",
                            f"{execution.name}/upload-impulse/normal/run001/requests.csv",
                            f"{execution.name}/upload-impulse/normal/run001/collectors/api_worker.parquet",
                        },
                    )
                    self.assertEqual(
                        bundle.read(f"{execution.name}/report.html"), b"detailed report"
                    )
                second = CliRunner().invoke(cli, args)
                self.assertEqual(second.exit_code, 0, second.output)
                self.assertIn("Existing Archives:\n" + str(archive), second.output)
                (run_dir / "new_source.csv").write_text("new data")
                third = CliRunner().invoke(cli, args)
                self.assertEqual(third.exit_code, 0, third.output)
                self.assertIn("New Archives:\n" + str(archive), third.output)
                with zipfile.ZipFile(archive) as bundle:
                    self.assertIn(
                        f"{execution.name}/upload-impulse/normal/run001/new_source.csv",
                        bundle.namelist(),
                    )

    def test_report_command_selects_one_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            results = Path(tmp)
            selected = results / "maven-pull-through__short-impulse__018"
            other = results / "maven-pull-through__short-impulse__019"
            selected.mkdir()
            other.mkdir()
            with (
                patch("pulp_juicer.cli.render_report") as detailed,
                patch("pulp_juicer.cli.render_trial_report") as trials,
            ):
                detailed.return_value = selected / "report.html"
                trials.return_value = selected / "trials.html"
                response = CliRunner().invoke(
                    cli, ["report", selected.name, "--results", str(results)]
                )
            self.assertEqual(response.exit_code, 0, response.output)
            detailed.assert_called_once_with(selected, "quarto")
            trials.assert_called_once_with(selected, "quarto")
            self.assertIn(str(selected / "report.html"), response.output)
            self.assertNotIn(str(other), response.output)

            missing = CliRunner().invoke(
                cli,
                [
                    "report",
                    "maven-pull-through__short-impulse__020",
                    "--results",
                    str(results),
                ],
            )
            self.assertNotEqual(missing.exit_code, 0)
            self.assertIn("Execution not found", missing.output)

            invalid = CliRunner().invoke(
                cli,
                [
                    "report",
                    "../maven-pull-through__short-impulse__018",
                    "--results",
                    str(results),
                ],
            )
            self.assertNotEqual(invalid.exit_code, 0)
            self.assertIn("Invalid execution ID", invalid.output)

    def test_report_command_renders_selected_execution_reports(self):
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
                (run_dir / "collectors").mkdir()
                pq.write_table(
                    pa.table({"timestamp_utc": [datetime(2026, 1, 1, tzinfo=UTC)]}),
                    run_dir / "collectors" / "timestamps.parquet",
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
                responses = [
                    CliRunner().invoke(
                        cli, ["report", execution.name, "--results", str(results)]
                    )
                    for execution in (first, second, existing)
                ]

            for execution, response in zip((first, second, existing), responses):
                self.assertEqual(response.exit_code, 0, response.output)
                self.assertEqual(
                    response.output.splitlines(),
                    [
                        "New Reports:",
                        str(execution / "report.html"),
                        str(execution / "trials.html"),
                    ],
                )
            for execution in (first, second):
                document = (execution / "report.qmd").read_text()
                self.assertIn(execution.name, document)
                self.assertIn(
                    f"| {execution.name} | burst | run001 | normal | 2 | 1 |",
                    document,
                )
                self.assertIn(
                    "[timestamps collector Parquet](burst/normal/run001/collectors/timestamps.parquet)",
                    document,
                )
            self.assertIn("# Summaries", (existing / "report.qmd").read_text())
            self.assertEqual((existing / "report.html").read_text(), "<html>new</html>")
            self.assertEqual(render.call_count, 6)

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
                write_postgres_samples(run_dir, cpu)
                write_content_samples(run_dir)
                write_api_samples(run_dir)
                if scenario == "burst" and run_id in ("run001", "run002", "run003"):
                    start = datetime(2026, 1, 1, tzinfo=UTC)
                    pq.write_table(
                        pa.Table.from_pylist(
                            [
                                {
                                    "task_id": f"{run_id}-task",
                                    "task_type": "maven.pull",
                                    "status": "completed",
                                    "created_at_utc": start
                                    - timedelta(
                                        milliseconds=100 if run_id == "run001" else 200
                                    ),
                                    "unblocked_at_utc": start
                                    - timedelta(milliseconds=40),
                                    "started_at_utc": start,
                                    "finished_at_utc": start
                                    + timedelta(milliseconds=230),
                                    "execution_time_us": 230000,
                                    "queue_wait_us": 100000
                                    if run_id == "run001"
                                    else 200000,
                                    "time_to_unblock_us": 60000
                                    if run_id == "run001"
                                    else 160000,
                                    "ready_wait_us": 40000,
                                },
                                *(
                                    [
                                        {
                                            "task_id": f"{run_id}-extra",
                                            "task_type": "maven.pull",
                                            "status": "completed",
                                            "created_at_utc": start,
                                            "unblocked_at_utc": start,
                                            "started_at_utc": start,
                                            "finished_at_utc": start
                                            + timedelta(milliseconds=50),
                                            "execution_time_us": 50000,
                                            "queue_wait_us": 0,
                                            "time_to_unblock_us": 0,
                                            "ready_wait_us": 0,
                                        }
                                    ]
                                    if run_id == "run002"
                                    else []
                                ),
                            ],
                            schema=TASK_SCHEMA,
                        ),
                        run_dir / "collectors" / "tasking.parquet",
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
            self.assertIn("### Content-worker I/O", source)
            self.assertIn("### API-worker I/O", source)
            self.assertIn("Mean API CPU (%)", source)
            self.assertIn("### Pulp temporary files", source)
            self.assertIn("6.00\u00a0±\u00a00.00", source)
            self.assertLess(
                source.index("### Content-worker I/O"), source.index("# Graphs")
            )
            self.assertIn(
                "| Candidate | Trials | Tasks | Completed | Failed | Throughput (tasks/s) | Median execution (ms) | Total execution (ms) | Median queue wait (ms) | Median time to unblock (ms) | Median ready wait (ms) |",
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
            self.assertIn("trials_burst_unblock_histogram.svg", source)
            self.assertIn("trials_burst_ready_wait_histogram.svg", source)
            self.assertGreater(
                source.index("trials_burst_wait_boxplot.svg"),
                source.index("# Graphs"),
            )
            wait_svg = execution / "graphs" / "trials_burst_wait_boxplot.svg"
            ET.parse(wait_svg)
            self.assertIn("normal (n=2)", wait_svg.read_text())
            self.assertIn("staging (n=1)", wait_svg.read_text())
            self.assertIn(
                "run001: median 100.00 ms from 1 task waits", wait_svg.read_text()
            )
            self.assertIn(
                "Median creation-to-start wait per run (ms)", wait_svg.read_text()
            )
            self.assertIn("100.00 ms</text>", wait_svg.read_text())
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
                "memory",
                "content_read",
                "content_write",
                "api_cpu",
                "api_memory",
                "api_read",
                "api_write",
                "temp_bytes",
                "temp_files",
                "task_starts",
                "task_completions",
            ):
                svg = execution / "graphs" / f"trials_burst_{metric}.svg"
                self.assertTrue(svg.is_file())
                ET.parse(svg)
                self.assertIn("normal / run001", svg.read_text())
                self.assertIn(
                    "staging / run002"
                    if metric.startswith("task_")
                    else "staging / run004",
                    svg.read_text(),
                )
                self.assertIn(
                    'data-series="smoothed-mean"'
                    if metric == "throughput"
                    else 'data-series="median"',
                    svg.read_text(),
                )
                if metric == "throughput":
                    self.assertIn("normal smoothed mean", svg.read_text())
                if metric in ("content_read", "content_write", "api_read", "api_write"):
                    self.assertIn('data-series="request-completed"', svg.read_text())
                    self.assertIn('data-series="request-starts"', svg.read_text())
                    self.assertIn("Median completed requests (right)", svg.read_text())
                    self.assertIn("Median new requests/s (right)", svg.read_text())
                self.assertIn(
                    'stroke-opacity="0.22" data-series="run"', svg.read_text()
                )
            self.assertIn("Time since load start (s)", svg.read_text())
            for candidate, count in (("normal", 2), ("staging", 2)):
                gantt_name = f"trials_burst_{candidate}_task_gantt.svg"
                self.assertIn(gantt_name, source)
                self.assertGreater(source.index(gantt_name), source.index("# Graphs"))
                gantt = ET.parse(execution / "graphs" / gantt_name)
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
            self.assertTrue(
                (execution / "graphs" / "trials_calibrate_completed.svg").is_file()
            )
            with patch("pulp_juicer.trial_report.shutil.which") as which:
                self.assertEqual(render_trial_report(execution), report)
            which.assert_not_called()

            source_path = execution / "trials.qmd"
            source_path.write_text(
                source.replace("<!-- pulp-juicer-trial-layout: 28 -->", "")
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
                "<!-- pulp-juicer-trial-layout: 28 -->", source_path.read_text()
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
                    cli, ["report", execution.name, "--results", str(results)]
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
