"""Detect a quiet PostgreSQL process tree before starting a load test."""

import asyncio
import time

if __package__:
    from .postgres import PostgresSampler
else:
    from postgres import PostgresSampler


def wait_for_postgres_idle(
    timeout_seconds: float, sampler: PostgresSampler | None = None
) -> dict:
    """Require two consecutive one-second intervals with zero PostgreSQL CPU time."""
    sampler = sampler or PostgresSampler()
    deadline = time.monotonic() + timeout_seconds
    quiet_samples = 0
    last_sample = None
    while True:
        last_sample = sampler.sample()
        if last_sample["processes"] and last_sample["cpu_seconds"] == 0:
            quiet_samples += 1
            if quiet_samples == 2:
                return last_sample
        else:
            quiet_samples = 0
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(
                "PostgreSQL did not become idle after "
                f"{timeout_seconds:g}s; last sample: {last_sample}"
            )
        time.sleep(min(1.0, remaining))


async def wait_for_postgres_idle_async(timeout_seconds: float) -> dict:
    """Keep periodic collectors running while the readiness check waits."""
    return await asyncio.to_thread(wait_for_postgres_idle, timeout_seconds)
