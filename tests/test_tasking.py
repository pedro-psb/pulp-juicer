"""Database task snapshots feed task reports without parsing Compose logs."""

import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pyarrow as pa
import pyarrow.parquet as pq

from pulp_juicer.collector.task_db import TASK_SCHEMA, task_row, write_task_snapshot
from pulp_juicer.tasking import task_report_sections, wait_distribution_chart


class TestTasking(unittest.TestCase):
    def test_task_row_splits_creation_to_start_delay(self):
        created = datetime(2026, 1, 1, tzinfo=UTC)
        row = task_row(
            (
                "task-1",
                "maven.pull",
                "default",
                "failed",
                False,
                True,
                created,
                created + timedelta(milliseconds=60),
                created + timedelta(milliseconds=100),
                created + timedelta(milliseconds=350),
                {"description": "upstream failed"},
            )
        )
        self.assertEqual(row["time_to_unblock_us"], 60000)
        self.assertEqual(row["ready_wait_us"], 40000)
        self.assertEqual(row["queue_wait_us"], 100000)
        self.assertEqual(row["execution_time_us"], 250000)
        self.assertEqual(row["failure_reason"], "upstream failed")
        unfinished = task_row(
            (
                "task-2",
                "maven.pull",
                "default",
                "waiting",
                False,
                True,
                created,
                None,
                None,
                None,
                None,
            )
        )
        self.assertIsNone(unfinished["ready_wait_us"])
        self.assertIsNone(unfinished["execution_time_us"])

    def test_on_demand_snapshot_writes_database_rows_to_parquet(self):
        created = datetime(2026, 1, 1, tzinfo=UTC)
        cursor = MagicMock()
        cursor.fetchall.return_value = [
            (
                "task-1",
                "maven.pull",
                "default",
                "completed",
                False,
                True,
                created,
                created,
                created + timedelta(milliseconds=25),
                created + timedelta(milliseconds=75),
                None,
            )
        ]
        connection = MagicMock()
        connection.__enter__.return_value.cursor.return_value.__enter__.return_value = (
            cursor
        )
        fake_psycopg = SimpleNamespace(connect=MagicMock(return_value=connection))
        components = SimpleNamespace(
            database=SimpleNamespace(uri="postgresql://pulp@/pulp")
        )
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(sys.modules, {"psycopg": fake_psycopg}),
        ):
            output = write_task_snapshot(Path(tmp), components)
            table = pq.read_table(output)
        self.assertEqual(table.schema, TASK_SCHEMA)
        self.assertEqual(table.num_rows, 1)
        self.assertEqual(table["ready_wait_us"].to_pylist(), [25000])
        cursor.execute.assert_called_once()
        self.assertIn("FROM core_task", cursor.execute.call_args.args[0])

    def test_reports_split_wait_histograms_and_keep_logs_out_of_analysis(self):
        with tempfile.TemporaryDirectory() as tmp:
            execution = Path(tmp) / "smoke__001"
            run_dir = execution / "burst" / "normal" / "run001"
            collector_dir = run_dir / "collectors"
            collector_dir.mkdir(parents=True)
            created = datetime(2026, 1, 1, tzinfo=UTC)
            rows = [
                task_row(
                    (
                        "task-1",
                        "maven.pull",
                        "default",
                        "completed",
                        False,
                        True,
                        created,
                        created + timedelta(milliseconds=60),
                        created + timedelta(milliseconds=100),
                        created + timedelta(milliseconds=300),
                        None,
                    )
                ),
                task_row(
                    (
                        "task-2",
                        "maven.pull",
                        "default",
                        "waiting",
                        False,
                        True,
                        created,
                        None,
                        None,
                        None,
                        None,
                    )
                ),
            ]
            pq.write_table(
                pa.Table.from_pylist(rows, schema=TASK_SCHEMA),
                collector_dir / "tasking.parquet",
            )
            (run_dir / "compose.log").write_text(
                "not task syntax; kept for debugging\n"
            )
            summary, distribution, timeline = task_report_sections(run_dir, execution)
            source = "\n".join(summary)
            self.assertIn("| Tasks observed | 2 |", source)
            self.assertIn("| Median time to unblock | 60.00 ms |", source)
            self.assertIn("| Median ready queue wait | 40.00 ms |", source)
            self.assertIn("report_task_unblock.svg", "\n".join(distribution))
            self.assertIn("report_task_ready_wait.svg", "\n".join(distribution))
            ET.parse(run_dir / "report_task_unblock.svg")
            ET.parse(run_dir / "report_task_ready_wait.svg")
            self.assertIn("report_tasks.svg", "\n".join(timeline))
            self.assertIn("dispatched, started, and completed", "\n".join(timeline))
            task_chart = ET.parse(run_dir / "report_tasks.svg")
            series = {
                line.attrib["data-series"]: line.attrib["points"]
                for line in task_chart.iter("{http://www.w3.org/2000/svg}polyline")
            }
            self.assertEqual(set(series), {"Dispatched", "Started", "Completed"})
            dispatch_y = float(series["Dispatched"].split()[0].split(",")[1])
            start_y = float(series["Started"].split()[0].split(",")[1])
            self.assertLess(dispatch_y, start_y)
            self.assertEqual(
                (run_dir / "compose.log").read_text(),
                "not task syntax; kept for debugging\n",
            )

    def test_wait_histogram_keeps_shared_bucket_boundaries(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "wait.svg"
            self.assertTrue(
                wait_distribution_chart(
                    path,
                    [
                        ("normal", "#2563eb", [10, 20, 20, 40], 8),
                        ("staging", "#e56b35", [10, 40, 40], 7),
                    ],
                    "Ready queue wait",
                )
            )
            svg = path.read_text()
            self.assertIn("Ready queue wait (ms)", svg)
            self.assertIn("normal: 4 of 8 tasks have wait data", svg)
            self.assertIn("staging: 3 of 7 tasks have wait data", svg)
            self.assertIn("10–20</text>", svg)
            tree = ET.parse(path)
            bars = [
                node
                for node in tree.iter("{http://www.w3.org/2000/svg}rect")
                if "data-count" in node.attrib
            ]
            self.assertEqual(sum(int(bar.attrib["data-count"]) for bar in bars), 7)
