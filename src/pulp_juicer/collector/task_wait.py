"""Wait for Pulp tasks to finish before the final task snapshot.

Future schedules live in ``core_taskschedule``. A schedule's currently
dispatched task is linked through ``last_task_id`` and does not hold up a run.
"""

import asyncio
import time
from collections.abc import Callable

if __package__:
    from .components import PulpComponents
else:
    from components import PulpComponents

TASK_STATUS_QUERY = """
    SELECT t.state, count(*)
    FROM core_task AS t
    WHERE NOT EXISTS (
          SELECT 1 FROM core_taskschedule AS s WHERE s.last_task_id = t.pulp_id
      )
    GROUP BY t.state
"""


def wait_for_tasks(
    timeout_seconds: float,
    components: PulpComponents | None = None,
    on_progress: Callable[[dict], None] | None = None,
) -> dict[str, int]:
    """Poll until non-scheduled tasks have reached a final state."""
    import psycopg

    components = components or PulpComponents()
    deadline = time.monotonic() + timeout_seconds
    last_report = -5.0
    with psycopg.connect(
        components.database.uri, connect_timeout=5, autocommit=True
    ) as connection:
        while True:
            with connection.cursor() as cursor:
                cursor.execute(TASK_STATUS_QUERY)
                counts = dict(cursor.fetchall())
            active = {
                state: counts[state]
                for state in ("waiting", "running", "canceling")
                if counts.get(state)
            }
            remaining = deadline - time.monotonic()
            elapsed = max(0.0, timeout_seconds - remaining)
            if on_progress and (last_report < 0 or elapsed - last_report >= 5):
                on_progress(
                    {
                        "elapsed_seconds": round(elapsed),
                        **{
                            state: counts.get(state, 0)
                            for state in (
                                "waiting",
                                "running",
                                "canceling",
                                "completed",
                                "failed",
                            )
                        },
                    }
                )
                last_report = elapsed
            if not active:
                return active
            if remaining <= 0:
                raise TimeoutError(
                    "Pulp tasks still active after "
                    f"{timeout_seconds:g}s: "
                    + ", ".join(
                        f"{state}={count}" for state, count in sorted(active.items())
                    )
                )
            time.sleep(min(1.0, remaining))


async def wait_for_tasks_async(
    timeout_seconds: float, on_progress: Callable[[dict], None] | None = None
) -> dict[str, int]:
    """Keep the collector control loop and periodic sampling responsive."""
    return await asyncio.to_thread(wait_for_tasks, timeout_seconds, None, on_progress)
