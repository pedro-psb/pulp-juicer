"""Detect a quiet PostgreSQL process tree before starting a load test."""

import asyncio
import time
from collections.abc import Callable

if __package__:
    from .postgres import PostgresSampler
else:
    from postgres import PostgresSampler


def wait_for_postgres_idle(
    timeout_seconds: float,
    sampler: PostgresSampler | None = None,
    on_progress: Callable[[dict], None] | None = None,
) -> dict:
    """Require two consecutive one-second intervals with zero PostgreSQL CPU time."""
    sampler = sampler or PostgresSampler()
    deadline = time.monotonic() + timeout_seconds
    quiet_samples = 0
    last_sample = None
    last_report = -5.0
    while True:
        last_sample = sampler.sample()
        if last_sample["processes"] and last_sample["cpu_seconds"] == 0:
            quiet_samples += 1
            if quiet_samples == 2:
                return last_sample
        else:
            quiet_samples = 0
        remaining = deadline - time.monotonic()
        elapsed = max(0.0, timeout_seconds - remaining)
        if on_progress and (last_report < 0 or elapsed - last_report >= 5):
            on_progress(
                {
                    "elapsed_seconds": round(elapsed),
                    "cpu_pct": last_sample["cpu_pct"],
                    "processes": last_sample["processes"],
                    "quiet_samples": quiet_samples,
                }
            )
            last_report = elapsed
        if remaining <= 0:
            raise TimeoutError(
                "PostgreSQL did not become idle after "
                f"{timeout_seconds:g}s; last sample: {last_sample}"
            )
        time.sleep(min(1.0, remaining))


async def wait_for_postgres_idle_async(
    timeout_seconds: float, on_progress: Callable[[dict], None] | None = None
) -> dict:
    """Keep periodic collectors running while the readiness check waits."""
    return await asyncio.to_thread(
        wait_for_postgres_idle, timeout_seconds, None, on_progress
    )
