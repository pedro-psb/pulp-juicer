"""Capture Pulp task state from PostgreSQL when the runner ends a test.

Each row is one task in this run's fresh Pulp database. ``pulp_created`` is the
creation time, ``unblocked_at`` is when Pulp found the task ready for pickup,
and ``started_at``/``finished_at`` mark execution. Time to unblock and ready
queue wait are distinct parts of the creation-to-start delay. An unfinished
task retains its current state and has null timestamps for phases not reached.
"""

import asyncio
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

if __package__:
    from .components import PulpComponents
else:
    from components import PulpComponents

TASK_SCHEMA = pa.schema(
    [
        pa.field("task_id", pa.string(), nullable=False),
        pa.field("task_type", pa.string()),
        pa.field("domain", pa.string()),
        pa.field("status", pa.string(), nullable=False),
        pa.field("immediate", pa.bool_()),
        pa.field("deferred", pa.bool_()),
        pa.field("created_at_utc", pa.timestamp("us", tz="UTC")),
        pa.field("unblocked_at_utc", pa.timestamp("us", tz="UTC")),
        pa.field("started_at_utc", pa.timestamp("us", tz="UTC")),
        pa.field("finished_at_utc", pa.timestamp("us", tz="UTC")),
        pa.field("time_to_unblock_us", pa.int64()),
        pa.field("ready_wait_us", pa.int64()),
        pa.field("queue_wait_us", pa.int64()),
        pa.field("execution_time_us", pa.int64()),
        pa.field("failure_reason", pa.string()),
    ],
    metadata={b"source": b"pulpcore.task", b"duration_unit": b"microseconds"},
)

TASK_QUERY = """
    SELECT t.pulp_id::text, t.name, d.name, t.state, t.immediate, t.deferred,
           t.pulp_created, t.unblocked_at, t.started_at, t.finished_at, t.error
    FROM core_task AS t
    LEFT JOIN core_domain AS d ON d.pulp_id = t.pulp_domain_id
    ORDER BY t.pulp_created, t.pulp_id
"""


def _elapsed_us(start, end):
    return (
        max(0, round((end - start).total_seconds() * 1_000_000))
        if start and end
        else None
    )


def task_row(record: tuple) -> dict:
    """Convert one DB row into task phases without inventing missing times."""
    (
        task_id,
        name,
        domain,
        state,
        immediate,
        deferred,
        created,
        unblocked,
        started,
        finished,
        error,
    ) = record
    return {
        "task_id": task_id,
        "task_type": name,
        "domain": domain,
        "status": state,
        "immediate": immediate,
        "deferred": deferred,
        "created_at_utc": created,
        "unblocked_at_utc": unblocked,
        "started_at_utc": started,
        "finished_at_utc": finished,
        "time_to_unblock_us": _elapsed_us(created, unblocked),
        "ready_wait_us": _elapsed_us(unblocked, started),
        "queue_wait_us": _elapsed_us(created, started),
        "execution_time_us": _elapsed_us(started, finished),
        "failure_reason": error.get("description") if isinstance(error, dict) else None,
    }


def write_task_snapshot(
    output_dir: Path, components: PulpComponents | None = None
) -> Path:
    """Query all current tasks and close a typed Parquet file before teardown."""
    import psycopg

    components = components or PulpComponents()
    with psycopg.connect(components.database.uri, connect_timeout=5) as connection:
        with connection.cursor() as cursor:
            cursor.execute(TASK_QUERY)
            rows = [task_row(record) for record in cursor.fetchall()]
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / "tasking.parquet"
    temporary = output.with_suffix(".parquet.tmp")
    try:
        pq.write_table(
            pa.Table.from_pylist(rows, schema=TASK_SCHEMA),
            temporary,
            compression="zstd",
        )
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    return output


async def collect_tasks(output_dir: Path) -> None:
    """Run the DB snapshot off the collector's async control loop."""
    await asyncio.to_thread(write_task_snapshot, output_dir)
