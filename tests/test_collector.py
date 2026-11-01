"""Parquet collector output and control replies."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from pulp_juicer.collector.collector import (
    COLLECTORS,
    TIMESTAMP_FIELD,
    Collector,
    handle_control,
    run_collector,
)


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
    def test_triggered_collector_replies_after_writing_random_parquet(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            response = asyncio.run(
                send_request(directory, {"command": "collect", "collector": "random"})
            )
            self.assertEqual(response, {"status": "finished", "collector": "random"})
            table = pq.read_table(directory / "random.parquet")
            self.assertEqual(table.num_rows, 10)
            self.assertEqual(table.schema.field("value").type, pa.int32())
            self.assertTrue(
                all(0 <= value < 1000 for value in table["value"].to_pylist())
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
