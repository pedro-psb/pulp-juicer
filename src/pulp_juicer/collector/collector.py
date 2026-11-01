"""Run independent periodic and on-demand Parquet collectors inside Pulp."""

import asyncio
import json
import os
import signal
import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

if __package__:
    from .components import PulpComponents
    from .content_io import ContentIOSampler
    from .content_temp import PulpTempSampler
    from .postgres import PostgresSampler
    from .postgres_wait import wait_for_postgres_idle_async
    from .task_db import collect_tasks
    from .task_wait import wait_for_tasks_async
else:
    from components import PulpComponents
    from content_io import ContentIOSampler
    from content_temp import PulpTempSampler
    from postgres import PostgresSampler
    from postgres_wait import wait_for_postgres_idle_async
    from task_db import collect_tasks
    from task_wait import wait_for_tasks_async

OUTPUT_DIR = Path(os.environ.get("JUICER_COLLECTOR_DIR", "/var/lib/pgsql/juicer"))
CONTROL_SOCKET = Path(
    os.environ.get("JUICER_COLLECTOR_SOCKET", "/tmp/pulp-juicer-collector.sock")
)


@dataclass(frozen=True)
class Collector:
    """A Parquet stream whose sample may contain one or several measurements."""

    name: str
    schema: pa.Schema
    sample: Callable[
        [],
        Awaitable[
            Mapping[str, str | int | float | datetime | None]
            | Sequence[Mapping[str, str | int | float | datetime | None]]
        ],
    ]


async def sample_timestamp() -> dict:
    """Record the sampling clock independently of any Pulp component."""
    return {}


TIMESTAMP_FIELD = pa.field("timestamp_utc", pa.timestamp("us", tz="UTC"))
COMPONENTS = PulpComponents()
POSTGRES_SAMPLER = PostgresSampler(COMPONENTS)
CONTENT_IO_SAMPLER = ContentIOSampler(COMPONENTS)
PULP_TEMP_SAMPLER = PulpTempSampler(COMPONENTS)


async def sample_postgres() -> dict:
    """Record PostgreSQL process-tree CPU and proportional memory usage."""
    return await asyncio.to_thread(POSTGRES_SAMPLER.sample)


async def sample_content_io() -> list[dict]:
    """Record storage-byte and read/write syscall rates for each content worker."""
    return await asyncio.to_thread(CONTENT_IO_SAMPLER.sample)


async def sample_pulp_temp() -> dict:
    """Record live files and bytes in Pulp's shared working directory."""
    return await asyncio.to_thread(PULP_TEMP_SAMPLER.sample)


COLLECTORS = (
    Collector("timestamps", pa.schema([TIMESTAMP_FIELD]), sample_timestamp),
    Collector(
        "postgres",
        pa.schema(
            [
                TIMESTAMP_FIELD,
                pa.field("interval_s", pa.float64()),
                pa.field("cpu_pct", pa.float64()),
                pa.field("cpu_seconds", pa.float64()),
                pa.field("pss_bytes", pa.int64()),
                pa.field("processes", pa.int32()),
            ]
        ),
        sample_postgres,
    ),
    Collector(
        "content_io",
        pa.schema(
            [
                TIMESTAMP_FIELD,
                pa.field("worker_pid", pa.int32()),
                pa.field("worker_start_utc", pa.timestamp("us", tz="UTC")),
                pa.field("interval_s", pa.float64()),
                pa.field("storage_read_bytes_per_s", pa.float64()),
                pa.field("storage_write_bytes_per_s", pa.float64()),
                pa.field("read_syscalls_per_s", pa.float64()),
                pa.field("write_syscalls_per_s", pa.float64()),
            ]
        ),
        sample_content_io,
    ),
    Collector(
        "pulp_temp",
        pa.schema(
            [
                TIMESTAMP_FIELD,
                pa.field("active_files", pa.int64()),
                pa.field("active_bytes", pa.int64()),
                pa.field("allocated_bytes", pa.int64()),
                pa.field("root_files", pa.int64()),
                pa.field("root_bytes", pa.int64()),
                pa.field("peak_bytes", pa.int64()),
            ]
        ),
        sample_pulp_temp,
    ),
)
TRIGGERED_COLLECTORS = {"tasking": collect_tasks}


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
        elif command == "wait-tasks":
            async with lock:
                counts = await wait_for_tasks_async(float(request["timeout_seconds"]))
            response = {"status": "finished", "command": command, "active": counts}
        elif command == "wait-postgres-idle":
            async with lock:
                sample = await wait_for_postgres_idle_async(
                    float(request["timeout_seconds"])
                )
            response = {"status": "finished", "command": command, "sample": sample}
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
                try:
                    values = await collector.sample()
                except Exception as exc:  # noqa: BLE001 - keep collecting after a failed sample
                    print(
                        f"Collector {collector.name} sample failed: {exc}",
                        file=sys.stderr,
                        flush=True,
                    )
                else:
                    timestamp = datetime.now(UTC)
                    samples = [values] if isinstance(values, Mapping) else values
                    rows.extend(
                        {"timestamp_utc": timestamp, **sample} for sample in samples
                    )
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
