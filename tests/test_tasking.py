"""Compose task logs become typed Parquet and report sections."""

import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

import pyarrow as pa
import pyarrow.parquet as pq
from click.testing import CliRunner

from pulp_juicer.cli import cli
from pulp_juicer.tasking import (
    TASK_SCHEMA,
    analyze_compose_log,
    analyze_execution_tasking,
    parse_tasking_log,
    task_report_sections,
    wait_distribution_chart,
)

START = (
    "pulp-1 | 2026-10-31T22:25:31.689107172Z pulp [cid]: "
    "pulpcore.tasking.tasks:INFO: Starting task id: {task_id} in domain: default, "
    "task_type: {task_type}, immediate: True, deferred: True, worker: 12@host\n"
)
DISPATCH = (
    "pulp-1 | 2026-10-31T22:25:31.600000000Z pulp [cid]: "
    "pulpcore.tasking._util:INFO: Dispatched scheduled task {task_type} "
    "as task id {task_id} in domain default\n"
)
COMPLETE = (
    "pulp-1 | 2026-10-31T22:25:31.920002178Z pulp [cid]: "
    "pulpcore.tasking.tasks:INFO: Task completed {task_id} in domain: default, "
    "task_type: {task_type}, immediate: True, deferred: True, execution_time: {duration} μs\n"
)
FAILURE = (
    "pulp-1 | 2026-10-31T22:25:31.920002178Z pulp [cid]: "
    "pulpcore.tasking.tasks:INFO: Task[{task_type}] {task_id} failed "
    "(ValueError: bad input) in domain: default\n"
)


class TestTasking(unittest.TestCase):
    def test_wait_histogram_uses_shared_rounded_buckets(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "wait.svg"
            self.assertTrue(
                wait_distribution_chart(
                    path,
                    [
                        ("normal", "#2563eb", [10, 20, 20, 40], 8),
                        ("staging", "#e56b35", [10, 40, 40], 7),
                    ],
                )
            )
            tree = ET.parse(path)
            svg = path.read_text()
            self.assertIn("Dispatch-to-start wait (ms)", svg)
            self.assertIn("Tasks with wait data (count)", svg)
            self.assertIn("normal: 4 of 8 tasks have wait data", svg)
            self.assertIn("staging: 3 of 7 tasks have wait data", svg)
            self.assertIn("10–20</text>", svg)
            bucket = next(
                node
                for node in tree.iter("{http://www.w3.org/2000/svg}text")
                if node.text == "10–20"
            )
            self.assertEqual(bucket.attrib["x"], "376.0")
            bars = [
                node
                for node in tree.iter("{http://www.w3.org/2000/svg}rect")
                if "data-count" in node.attrib
            ]
            for color, count in (("#2563eb", 4), ("#e56b35", 3)):
                self.assertEqual(
                    sum(
                        int(node.attrib["data-count"])
                        for node in bars
                        if node.attrib["fill"] == color
                    ),
                    count,
                )
            self.assertEqual(len(tree.findall("{http://www.w3.org/2000/svg}rect")), 8)

    def test_task_lifecycle_is_joined_and_parquet_is_typed_and_cached(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "results" / "smoke__001" / "burst" / "normal" / "run001"
            run_dir.mkdir(parents=True)
            log = run_dir / "compose.log"
            log.write_text(
                "fixture-1 | 2026-10-31T22:25:31Z GET /artifact 200\n"
                + DISPATCH.format(task_id="one", task_type="maven.pull")
                + START.format(task_id="one", task_type="maven.pull")
                + COMPLETE.format(
                    task_id="one", task_type="maven.pull", duration=230000
                )
                + START.format(task_id="two", task_type="core.cleanup")
                + FAILURE.format(task_id="two", task_type="core.cleanup")
                + COMPLETE.format(
                    task_id="three", task_type="maven.pull", duration=50000
                )
            )
            parsed = parse_tasking_log(log)
            self.assertEqual(len(parsed), 3)
            by_id = {row["task_id"]: row for row in parsed}
            self.assertEqual(by_id["one"]["status"], "completed")
            self.assertEqual(by_id["one"]["execution_time_us"], 230000)
            self.assertEqual(by_id["one"]["observed_elapsed_us"], 230895)
            self.assertEqual(by_id["one"]["waiting_time_us"], 89107)
            self.assertEqual(by_id["two"]["status"], "failed")
            self.assertEqual(by_id["two"]["failure_reason"], "ValueError: bad input")
            self.assertIsNone(by_id["three"]["started_at_utc"])

            output, changed = analyze_compose_log(run_dir)
            self.assertTrue(changed)
            self.assertEqual(output, run_dir / "tasking.parquet")
            table = pq.read_table(output)
            self.assertEqual(table.schema, TASK_SCHEMA)
            self.assertEqual(table.num_rows, 3)
            self.assertEqual(table.schema.field("execution_time_us").type, pa.int64())
            self.assertEqual(
                table.schema.field("started_at_utc").type,
                pa.timestamp("us", tz="UTC"),
            )
            self.assertEqual(analyze_compose_log(run_dir), (output, False))
            self.assertFalse(analyze_execution_tasking(run_dir.parents[2]))
            pq.write_table(table.drop(["dispatched_at_utc", "waiting_time_us"]), output)
            self.assertEqual(analyze_compose_log(run_dir), (output, True))
            self.assertEqual(pq.read_table(output).schema, TASK_SCHEMA)

            (run_dir / "run_metadata.json").write_text(
                '{"result":{"load_start_utc":"2026-10-31T22:25:31+00:00"}}'
            )
            summary, distribution, timeline = task_report_sections(
                run_dir, run_dir.parents[2]
            )
            source = "\n".join(summary)
            self.assertIn("| Tasks observed | 3 |", source)
            self.assertIn("| Completed | 2 |", source)
            self.assertIn("| Failed | 1 |", source)
            self.assertIn("| Tasks with dispatch and start logs | 1 |", source)
            self.assertIn("| Median dispatch-to-start wait | 89.11 ms |", source)
            self.assertIn("| Task throughput |", source)
            self.assertNotIn("report_task_wait.svg", source)
            self.assertIn("report_task_wait.svg", "\n".join(distribution))
            ET.parse(run_dir / "report_task_wait.svg")
            self.assertIn(
                "This run: 1 of 3 tasks have wait data",
                (run_dir / "report_task_wait.svg").read_text(),
            )
            self.assertIn(
                "Tasks with wait data (count)",
                (run_dir / "report_task_wait.svg").read_text(),
            )
            self.assertNotIn(
                "<polyline", (run_dir / "report_task_wait.svg").read_text()
            )
            self.assertNotIn(
                "Tasks at or below wait", (run_dir / "report_task_wait.svg").read_text()
            )
            self.assertNotIn("Started without terminal record", source)
            self.assertIn("| Completed without start record | 1 |", source)
            frequent = source.split("##### Most frequent task types", 1)[1].split(
                "##### Total execution time by task type", 1
            )[0]
            self.assertIn("| maven.pull | 2 | 2 | 0 | 280.00 |", frequent)
            self.assertIn("| core.cleanup | 1 | 0 | 1 | N/A |", source)
            self.assertIn("Total execution time by task type", source)
            self.assertIn(
                "| Task type | Tasks | Completed | Failed | Total execution (ms) |",
                source,
            )
            self.assertIn("| maven.pull | 2 | 2 | 0 | 280.00 |", source)
            self.assertNotIn("report_tasks.svg", source)
            self.assertIn("report_tasks.svg", "\n".join(timeline))
            ET.parse(run_dir / "report_tasks.svg")
            self.assertIn(
                "Time since load start (s)", (run_dir / "report_tasks.svg").read_text()
            )

    def test_report_analyzes_logs_and_refreshes_stale_detailed_html(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            execution = root / "results" / "smoke__001"
            run_dir = execution / "burst" / "normal" / "run001"
            run_dir.mkdir(parents=True)
            (run_dir / "locust_stats.csv").write_text(
                "Name,Request Count,Failure Count,Requests/s,Average Response Time,95%\n"
                "Aggregated,1,0,1.0,12,20\n"
            )
            (run_dir / "compose.log").write_text(
                DISPATCH.format(task_id="one", task_type="maven.pull")
                + START.format(task_id="one", task_type="maven.pull")
                + COMPLETE.format(
                    task_id="one", task_type="maven.pull", duration=230000
                )
            )
            (run_dir / "run_metadata.json").write_text(
                json.dumps(
                    {
                        "fixture": {
                            "count": 1,
                            "requested_count": 1,
                            "requested_fixture_id": "fixture",
                            "requested_payload_bytes": 10,
                            "manifest_sha256": "abc",
                        },
                        "load": {
                            "users": 1,
                            "spawn_rate": 1,
                            "duration": "1m",
                            "wait_seconds": 0,
                            "idle_seconds": 0,
                            "tail_seconds": 0,
                        },
                        "setup": {"base_url": "http://localhost/"},
                        "upstream_url": "http://fixture/",
                        "project": "test",
                        "sources": {},
                    }
                )
            )
            (execution / "report.html").write_text("old detailed report")
            (execution / "trials.html").write_text("existing trial report")

            def fake_quarto(command, **kwargs):
                output = command[command.index("--output") + 1]
                (kwargs["cwd"] / output).write_text("new detailed report")

            with (
                patch("pulp_juicer.report.shutil.which", return_value="/bin/true"),
                patch(
                    "pulp_juicer.report.subprocess.run", side_effect=fake_quarto
                ) as render,
            ):
                first = CliRunner().invoke(
                    cli, ["report", "--results", str(execution.parent)]
                )
                second = CliRunner().invoke(
                    cli, ["report", "--results", str(execution.parent)]
                )
                source_path = execution / "report.qmd"
                source_path.write_text(
                    source_path.read_text().replace(
                        "<!-- pulp-juicer-report-layout: 19 -->", ""
                    )
                )
                third = CliRunner().invoke(
                    cli, ["report", "--results", str(execution.parent)]
                )

            self.assertEqual(first.exit_code, 0, first.output)
            self.assertEqual(second.exit_code, 0, second.output)
            self.assertEqual(third.exit_code, 0, third.output)
            self.assertEqual(render.call_count, 3)
            self.assertIn(f"New Reports:\n{execution / 'report.html'}", first.output)
            self.assertIn(str(execution / "trials.html"), first.output)
            self.assertIn(f"Existing:\n{execution / 'report.html'}", second.output)
            self.assertTrue((run_dir / "tasking.parquet").is_file())
            report_source = (execution / "report.qmd").read_text()
            self.assertIn("<!-- pulp-juicer-report-layout: 19 -->", report_source)
            self.assertIn("#### Pulp tasks", report_source)
            self.assertIn("Most frequent task types", report_source)
            self.assertIn("Total execution time by task type", report_source)
            self.assertIn("report_tasks.svg", report_source)
            self.assertIn("report_task_wait.svg", report_source)
            self.assertIn("tasking.parquet", report_source)
            self.assertLess(
                report_source.index("# Summaries"), report_source.index("# Graphs")
            )
            self.assertLess(
                report_source.index("# Graphs"),
                report_source.index("# Run configuration and source files"),
            )
            self.assertLess(
                report_source.index("##### Total execution time by task type"),
                report_source.index("# Graphs"),
            )
            self.assertGreater(
                report_source.index("report_task_wait.svg"),
                report_source.index("# Graphs"),
            )
            self.assertLess(
                report_source.index("report_tasks.svg"),
                report_source.index("# Run configuration and source files"),
            )
            self.assertGreater(
                report_source.index("### Run configuration"),
                report_source.index("# Run configuration and source files"),
            )
            self.assertGreater(
                report_source.index("### Source files"),
                report_source.index("# Run configuration and source files"),
            )
            trial_source = (execution / "trials.qmd").read_text()
            self.assertIn("### Pulp tasks", trial_source)
            self.assertIn("trials_burst_task_starts.svg", trial_source)
            self.assertIn("trials_burst_task_completions.svg", trial_source)
            self.assertIn("trials_burst_wait_boxplot.svg", trial_source)

    def test_incomplete_run_log_is_analyzed_without_locust_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            execution = Path(tmp) / "smoke__001"
            run_dir = execution / "burst" / "normal" / "run001"
            run_dir.mkdir(parents=True)
            (run_dir / "compose.log").write_text(
                START.format(task_id="one", task_type="maven.pull")
            )
            self.assertTrue(analyze_execution_tasking(execution))
            table = pq.read_table(run_dir / "tasking.parquet")
            self.assertEqual(table.num_rows, 1)
            self.assertEqual(table["status"].to_pylist(), ["started"])

            empty_run = execution / "burst" / "staging" / "run002"
            empty_run.mkdir(parents=True)
            (empty_run / "compose.log").write_text(
                "pulp-1 | 2026-10-31T22:25:31Z service is ready\n"
            )
            self.assertTrue(analyze_execution_tasking(execution))
            empty_table = pq.read_table(empty_run / "tasking.parquet")
            self.assertEqual(empty_table.num_rows, 0)
            self.assertEqual(empty_table.schema, TASK_SCHEMA)
            self.assertIn(
                "No Pulp task lifecycle records",
                "\n".join(task_report_sections(empty_run, execution)[0]),
            )
