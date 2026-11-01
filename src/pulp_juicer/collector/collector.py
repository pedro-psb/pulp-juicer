"""Run independent periodic and on-demand Parquet collectors inside Pulp."""

import asyncio
import json
import os
import random
import signal
import sys
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

OUTPUT_DIR = Path(os.environ.get("JUICER_COLLECTOR_DIR", "/var/lib/pulp/juicer"))
CONTROL_SOCKET = Path(
    os.environ.get("JUICER_COLLECTOR_SOCKET", "/tmp/pulp-juicer-collector.sock")
)


@dataclass(frozen=True)
class Collector:
    name: str
    schema: pa.Schema
    sample: Callable[[], Awaitable[Mapping[str, str | int | float]]]


async def sample_timestamp() -> dict:
    return {}


TIMESTAMP_FIELD = pa.field("timestamp_utc", pa.timestamp("us", tz="UTC"))
COLLECTORS = (Collector("timestamps", pa.schema([TIMESTAMP_FIELD]), sample_timestamp),)
RANDOM_SCHEMA = pa.schema([TIMESTAMP_FIELD, pa.field("value", pa.int32())])


async def write_random_entries(output_dir: Path) -> None:
    """Proof-of-concept collector that writes ten rows on demand."""
    output_dir.mkdir(parents=True, exist_ok=True)
    rows = [
        {"timestamp_utc": datetime.now(UTC), "value": random.randint(0, 999)}
        for _ in range(10)
    ]
    pq.write_table(
        pa.Table.from_pylist(rows, schema=RANDOM_SCHEMA),
        output_dir / "random.parquet",
        compression="zstd",
    )


TRIGGERED_COLLECTORS = {"random": write_random_entries}


async def handle_control(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    output_dir: Path,
    lock: asyncio.Lock,
    sampling_stop: asyncio.Event,
    periodic_tasks: list[asyncio.Task],
) -> None:
    """Reply only after a job or finalization has closed its Parquet files."""
    try:
        request = json.loads(await asyncio.wait_for(reader.readline(), timeout=5))
        command = request["command"]
        if command == "collect":
            name = request["collector"]
            collector = TRIGGERED_COLLECTORS[name]
            async with lock:
                await collector(output_dir)
            response = {"status": "finished", "collector": name}
        elif command == "finalize":
            sampling_stop.set()
            await asyncio.gather(*periodic_tasks)
            response = {"status": "finished", "command": "finalize"}
        else:
            raise ValueError(f"Unknown collector command: {command}")
    except Exception as exc:  # noqa: BLE001 - return errors to the caller
        response = {"status": "error", "message": f"{type(exc).__name__}: {exc}"}
    try:
        writer.write(json.dumps(response).encode() + b"\n")
        await writer.drain()
    finally:
        writer.close()
        await writer.wait_closed()


async def run_collector(
    collector: Collector,
    output_dir: Path,
    stop: asyncio.Event,
    interval_seconds: float = 1.0,
    batch_size: int = 60,
) -> None:
    """Sample on a monotonic schedule; one slow source cannot delay another."""
    output_dir.mkdir(parents=True, exist_ok=True)
    with pq.ParquetWriter(
        output_dir / f"{collector.name}.parquet", collector.schema, compression="zstd"
    ) as writer:
        rows = []
        loop = asyncio.get_running_loop()
        next_sample = loop.time()
        try:
            while not stop.is_set():
                timestamp = datetime.now(UTC)
                try:
                    values = await collector.sample()
                except Exception as exc:  # noqa: BLE001 - keep collecting after a failed sample
                    print(
                        f"Collector {collector.name} sample failed: {exc}",
                        file=sys.stderr,
                        flush=True,
                    )
                else:
                    rows.append({"timestamp_utc": timestamp, **values})
                    if len(rows) >= batch_size:
                        writer.write_table(
                            pa.Table.from_pylist(rows, schema=collector.schema)
                        )
                        rows.clear()
                next_sample += interval_seconds
                while next_sample <= loop.time():
                    next_sample += interval_seconds
                try:
                    await asyncio.wait_for(stop.wait(), next_sample - loop.time())
                except TimeoutError:
                    pass
        finally:
            if rows:
                writer.write_table(pa.Table.from_pylist(rows, schema=collector.schema))


async def main() -> None:
    process_stop = asyncio.Event()
    sampling_stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, process_stop.set)
    CONTROL_SOCKET.parent.mkdir(parents=True, exist_ok=True)
    CONTROL_SOCKET.unlink(missing_ok=True)
    lock = asyncio.Lock()
    try:
        async with asyncio.TaskGroup() as tasks:
            periodic_tasks = []
            for collector in COLLECTORS:
                periodic_tasks.append(
                    tasks.create_task(
                        run_collector(collector, OUTPUT_DIR, sampling_stop)
                    )
                )
            server = await asyncio.start_unix_server(
                lambda reader, writer: handle_control(
                    reader, writer, OUTPUT_DIR, lock, sampling_stop, periodic_tasks
                ),
                path=str(CONTROL_SOCKET),
            )
            async with server:
                await process_stop.wait()
            sampling_stop.set()
    finally:
        CONTROL_SOCKET.unlink(missing_ok=True)


if __name__ == "__main__":
    asyncio.run(main())
