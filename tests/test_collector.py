"""Parquet collector output and control replies."""

import asyncio
import json
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pyarrow as pa
import pyarrow.parquet as pq

from pulp_juicer.collector.collector import (
    COLLECTORS,
    TIMESTAMP_FIELD,
    TRIGGERED_COLLECTORS,
    Collector,
    handle_control,
    run_collector,
)
from pulp_juicer.collector.content_io import ContentIOSampler
from pulp_juicer.collector.content_temp import PulpTempSampler
from pulp_juicer.collector.postgres import PostgresSampler
from pulp_juicer.collector.postgres_snapshot import snapshot
from pulp_juicer.collector.postgres_wait import wait_for_postgres_idle
from pulp_juicer.collector.task_wait import wait_for_tasks


class Writer:
    def __init__(self):
        self.data = b""

    def write(self, data):
        self.data += data

    async def drain(self):
        pass

    def close(self):
        pass

    async def wait_closed(self):
        pass


async def send_request(directory, payload, stop=None, tasks=None):
    reader = asyncio.StreamReader()
    reader.feed_data(json.dumps(payload).encode() + b"\n")
    reader.feed_eof()
    writer = Writer()
    await handle_control(
        reader,
        writer,
        directory,
        asyncio.Lock(),
        stop or asyncio.Event(),
        tasks or [],
    )
    return json.loads(writer.data)


class TestCollectors(unittest.TestCase):
    def test_postgres_wait_requires_two_quiet_samples(self):
        sampler = SimpleNamespace(
            sample=MagicMock(
                side_effect=[
                    {"processes": 2, "cpu_seconds": None},
                    {"processes": 2, "cpu_seconds": 0},
                    {"processes": 2, "cpu_seconds": 0.01},
                    {"processes": 2, "cpu_seconds": 0},
                    {"processes": 2, "cpu_seconds": 0},
                ]
            )
        )
        with patch("pulp_juicer.collector.postgres_wait.time.sleep"):
            self.assertEqual(
                wait_for_postgres_idle(10, sampler),
                {"processes": 2, "cpu_seconds": 0},
            )
        self.assertEqual(sampler.sample.call_count, 5)

    def test_postgres_wait_times_out_if_process_is_unavailable(self):
        sampler = SimpleNamespace(
            sample=MagicMock(return_value={"processes": 0, "cpu_seconds": None})
        )
        with patch(
            "pulp_juicer.collector.postgres_wait.time.monotonic", side_effect=[0, 2]
        ):
            with self.assertRaisesRegex(TimeoutError, "did not become idle"):
                wait_for_postgres_idle(1, sampler)

    def test_task_wait_polls_active_states_until_cleared(self):
        components = SimpleNamespace(database=SimpleNamespace(uri="postgresql:///pulp"))
        connect = MagicMock()
        with (
            patch.dict("sys.modules", {"psycopg": SimpleNamespace(connect=connect)}),
            patch("pulp_juicer.collector.task_wait.time.sleep") as sleep,
        ):
            cursor = connect.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
            cursor.fetchall.side_effect = [
                [("waiting", 2), ("running", 1)],
                [("running", 1)],
                [],
            ]
            self.assertEqual(wait_for_tasks(10, components), {})
            self.assertEqual(cursor.execute.call_count, 3)
            sleep.assert_called()
            self.assertIn("core_task", cursor.execute.call_args.args[0])
            self.assertIn("core_taskschedule", cursor.execute.call_args.args[0])
            self.assertIn("last_task_id", cursor.execute.call_args.args[0])

    def test_task_wait_times_out_with_remaining_counts(self):
        components = SimpleNamespace(database=SimpleNamespace(uri="postgresql:///pulp"))
        connect = MagicMock()
        with (
            patch.dict("sys.modules", {"psycopg": SimpleNamespace(connect=connect)}),
            patch("pulp_juicer.collector.task_wait.time.monotonic", side_effect=[0, 2]),
        ):
            cursor = connect.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
            cursor.fetchall.return_value = [("waiting", 742), ("running", 1)]
            with self.assertRaisesRegex(TimeoutError, "waiting=742"):
                wait_for_tasks(1, components)

    def test_content_io_rates_follow_worker_identity(self):
        components = SimpleNamespace(content_workers=(SimpleNamespace(pid=11),))
        sampler = ContentIOSampler(components)
        first = [
            {
                "pid": 11,
                "create_time": 100.0,
                "read_bytes": 10,
                "write_bytes": 20,
                "read_count": 3,
                "write_count": 4,
            }
        ]
        second = [
            {
                **first[0],
                "read_bytes": 210,
                "write_bytes": 120,
                "read_count": 13,
                "write_count": 24,
            }
        ]
        restarted = [{**second[0], "create_time": 200.0}]
        with (
            patch.object(sampler, "_snapshot", side_effect=[first, second, restarted]),
            patch(
                "pulp_juicer.collector.content_io.time.monotonic", side_effect=[1, 3, 5]
            ),
        ):
            baseline = sampler.sample()[0]
            measured = sampler.sample()[0]
            new_worker = sampler.sample()[0]
        self.assertIsNone(baseline["storage_read_bytes_per_s"])
        self.assertEqual(measured["interval_s"], 2)
        self.assertEqual(measured["storage_read_bytes_per_s"], 100)
        self.assertEqual(measured["storage_write_bytes_per_s"], 50)
        self.assertEqual(measured["read_syscalls_per_s"], 5)
        self.assertEqual(measured["write_syscalls_per_s"], 10)
        self.assertIsNone(new_worker["storage_read_bytes_per_s"])

    def test_pulp_temp_counts_shared_tree_and_sampled_peak(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "download").write_bytes(b"abc")
            (root / "worker").mkdir()
            (root / "worker" / "task").write_bytes(b"12345")
            sampler = PulpTempSampler(SimpleNamespace(working_directory=root))
            first = sampler.sample()
            (root / "download").unlink()
            second = sampler.sample()
        self.assertEqual(first["active_files"], 2)
        self.assertEqual(first["active_bytes"], 8)
        self.assertEqual(first["root_files"], 1)
        self.assertEqual(first["root_bytes"], 3)
        self.assertEqual(second["active_files"], 1)
        self.assertEqual(second["active_bytes"], 5)
        self.assertEqual(second["peak_bytes"], 8)

    def test_content_io_collector_writes_one_row_per_worker(self):
        async def sample():
            return [
                {"worker_pid": 11},
                {"worker_pid": 12},
            ]

        async def exercise(directory):
            stop = asyncio.Event()
            task = asyncio.create_task(
                run_collector(
                    Collector("content_io", COLLECTORS[2].schema, sample),
                    directory,
                    stop,
                    interval_seconds=0.1,
                )
            )
            await asyncio.sleep(0.02)
            stop.set()
            await task

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            asyncio.run(exercise(directory))
            rows = pq.read_table(directory / "content_io.parquet").to_pylist()
        self.assertEqual([row["worker_pid"] for row in rows], [11, 12])
        self.assertEqual(rows[0]["timestamp_utc"], rows[1]["timestamp_utc"])

    def test_postgres_collector_writes_cpu_and_memory_parquet(self):
        async def sample():
            return {
                "interval_s": 1.0,
                "cpu_pct": 125.0,
                "cpu_seconds": 1.25,
                "pss_bytes": 64 * 2**20,
                "processes": 5,
            }

        async def exercise(directory):
            stop = asyncio.Event()
            task = asyncio.create_task(
                run_collector(
                    Collector("postgres", COLLECTORS[1].schema, sample),
                    directory,
                    stop,
                    interval_seconds=0.1,
                )
            )
            await asyncio.sleep(0.12)
            stop.set()
            await task

        with tempfile.TemporaryDirectory() as tmp:
            asyncio.run(exercise(Path(tmp)))
            rows = pq.read_table(Path(tmp) / "postgres.parquet").to_pylist()
            self.assertGreaterEqual(len(rows), 1)
            self.assertEqual(rows[0]["cpu_pct"], 125)
            self.assertEqual(rows[0]["pss_bytes"], 64 * 2**20)

    def test_postgres_snapshot_reads_descendants_and_pss(self):
        class Process:
            def __init__(self, pid, cpu, pss, children=()):
                self.pid, self.cpu, self.pss, self._children = pid, cpu, pss, children

            def children(self, recursive=False):
                self_outer.assertTrue(recursive)
                return self._children

            def oneshot(self):
                return nullcontext()

            def cpu_times(self):
                return SimpleNamespace(user=self.cpu, system=0)

            def create_time(self):
                return 1.0

            def memory_full_info(self):
                return SimpleNamespace(pss=self.pss)

        self_outer = self
        worker = Process(11, 3, 40)
        root = Process(10, 5, 60, [worker])
        with patch(
            "pulp_juicer.collector.postgres_snapshot.psutil.Process",
            return_value=root,
        ):
            rows = snapshot(10)
        self.assertEqual([row["pid"] for row in rows], [10, 11])
        self.assertEqual(sum(row["pss_bytes"] for row in rows), 100)

    def test_postgres_sampler_uses_component_pid_and_cpu_deltas(self):
        components = SimpleNamespace(postgres=SimpleNamespace(pid=10))
        sampler = PostgresSampler(components)
        first_rows = [
            {"pid": 10, "create_time": 1.0, "cpu_seconds": 5.0, "pss_bytes": 60},
            {"pid": 11, "create_time": 1.0, "cpu_seconds": 3.0, "pss_bytes": 40},
        ]
        second_rows = [
            {"pid": 10, "create_time": 1.0, "cpu_seconds": 6.0, "pss_bytes": 60},
            {"pid": 11, "create_time": 1.0, "cpu_seconds": 3.5, "pss_bytes": 40},
        ]
        with (
            patch.object(sampler, "_snapshot", side_effect=[first_rows, second_rows]),
            patch("pulp_juicer.collector.postgres.time.monotonic", side_effect=[1, 2]),
        ):
            first = sampler.sample()
            second = sampler.sample()
        self.assertIsNone(first["cpu_pct"])
        self.assertEqual(second["cpu_seconds"], 1.5)
        self.assertEqual(second["cpu_pct"], 150)
        self.assertEqual(second["pss_bytes"], 100)
        self.assertEqual(second["processes"], 2)

    def test_triggered_collector_replies_after_writing_task_parquet(self):
        async def write_tasks(directory):
            pq.write_table(
                pa.table({"task_id": ["one"]}), directory / "tasking.parquet"
            )

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            with patch.dict(TRIGGERED_COLLECTORS, {"tasking": write_tasks}):
                response = asyncio.run(
                    send_request(
                        directory, {"command": "collect", "collector": "tasking"}
                    )
                )
            self.assertEqual(response, {"status": "finished", "collector": "tasking"})
            self.assertEqual(
                pq.read_table(directory / "tasking.parquet")["task_id"].to_pylist(),
                ["one"],
            )

    def test_task_wait_control_reply(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch(
                "pulp_juicer.collector.collector.wait_for_tasks_async", return_value={}
            ) as wait:
                response = asyncio.run(
                    send_request(
                        Path(tmp),
                        {"command": "wait-tasks", "timeout_seconds": 10},
                    )
                )
            wait.assert_awaited_once_with(10.0)
            self.assertEqual(
                response,
                {"status": "finished", "command": "wait-tasks", "active": {}},
            )

    def test_postgres_idle_control_reply(self):
        sample = {"processes": 2, "cpu_seconds": 0}
        with tempfile.TemporaryDirectory() as tmp:
            with patch(
                "pulp_juicer.collector.collector.wait_for_postgres_idle_async",
                return_value=sample,
            ) as wait:
                response = asyncio.run(
                    send_request(
                        Path(tmp),
                        {"command": "wait-postgres-idle", "timeout_seconds": 10},
                    )
                )
            wait.assert_awaited_once_with(10.0)
            self.assertEqual(
                response,
                {
                    "status": "finished",
                    "command": "wait-postgres-idle",
                    "sample": sample,
                },
            )

    def test_finalize_reply_follows_readable_periodic_parquet(self):
        async def exercise(directory):
            stop = asyncio.Event()
            task = asyncio.create_task(
                run_collector(COLLECTORS[0], directory, stop, interval_seconds=0.1)
            )
            await asyncio.sleep(0.25)
            response = await send_request(
                directory, {"command": "finalize"}, stop, [task]
            )
            self.assertTrue(task.done())
            return response

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self.assertEqual(
                asyncio.run(exercise(directory)),
                {"status": "finished", "command": "finalize"},
            )
            table = pq.read_table(directory / "timestamps.parquet")
            self.assertGreaterEqual(table.num_rows, 2)
            self.assertEqual(
                table.schema.field("timestamp_utc").type, TIMESTAMP_FIELD.type
            )

    def test_slow_collector_does_not_delay_another_collector(self):
        async def fast_sample():
            return {"value": 1}

        async def slow_sample():
            await asyncio.sleep(0.25)
            return {"value": 2}

        schema = pa.schema([TIMESTAMP_FIELD, pa.field("value", pa.int32())])

        async def exercise(directory):
            stop = asyncio.Event()
            tasks = [
                asyncio.create_task(
                    run_collector(Collector(name, schema, sample), directory, stop, 0.1)
                )
                for name, sample in (("fast", fast_sample), ("slow", slow_sample))
            ]
            await asyncio.sleep(0.46)
            stop.set()
            await asyncio.gather(*tasks)

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            asyncio.run(exercise(directory))
            fast = pq.read_table(directory / "fast.parquet")
            slow = pq.read_table(directory / "slow.parquet")
            self.assertGreaterEqual(fast.num_rows, 4)
            self.assertGreater(fast.num_rows, slow.num_rows)
