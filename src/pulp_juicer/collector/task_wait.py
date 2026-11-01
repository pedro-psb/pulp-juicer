"""Wait for Pulp tasks to finish before the final task snapshot.

Future schedules live in ``core_taskschedule``. A schedule's currently
dispatched task is linked through ``last_task_id`` and does not hold up a run.
"""

import asyncio
import time

if __package__:
    from .components import PulpComponents
else:
    from components import PulpComponents

ACTIVE_TASK_QUERY = """
    SELECT t.state, count(*)
    FROM core_task AS t
    WHERE t.state IN ('waiting', 'running', 'canceling')
      AND NOT EXISTS (
          SELECT 1 FROM core_taskschedule AS s WHERE s.last_task_id = t.pulp_id
      )
    GROUP BY t.state
"""


def wait_for_tasks(
    timeout_seconds: float, components: PulpComponents | None = None
) -> dict[str, int]:
    """Poll until non-scheduled tasks have reached a final state."""
    import psycopg

    components = components or PulpComponents()
    deadline = time.monotonic() + timeout_seconds
    with psycopg.connect(
        components.database.uri, connect_timeout=5, autocommit=True
    ) as connection:
        while True:
            with connection.cursor() as cursor:
                cursor.execute(ACTIVE_TASK_QUERY)
                counts = dict(cursor.fetchall())
            if not counts:
                return counts
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    "Pulp tasks still active after "
                    f"{timeout_seconds:g}s: "
                    + ", ".join(
                        f"{state}={count}" for state, count in sorted(counts.items())
                    )
                )
            time.sleep(min(1.0, remaining))


async def wait_for_tasks_async(timeout_seconds: float) -> dict[str, int]:
    """Keep the collector control loop and periodic sampling responsive."""
    return await asyncio.to_thread(wait_for_tasks, timeout_seconds)
