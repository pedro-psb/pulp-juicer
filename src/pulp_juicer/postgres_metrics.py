"""Read PostgreSQL process-tree samples captured inside the Pulp container."""

from pathlib import Path

import pyarrow.parquet as pq


def read_postgres_samples(run_dir: Path) -> list[dict]:
    path = run_dir / "collectors" / "postgres.parquet"
    return pq.read_table(path).to_pylist() if path.is_file() else []


def summarize_postgres(samples: list[dict]) -> dict | None:
    active = [row for row in samples if row["cpu_pct"] is not None]
    memory = [row["pss_bytes"] for row in samples if row["pss_bytes"] is not None]
    if not active and not memory:
        return None
    duration = sum(row["interval_s"] or 0 for row in active)
    cpu_seconds = sum(row["cpu_seconds"] or 0 for row in active)
    return {
        "cpu_mean_pct": cpu_seconds / duration * 100 if duration else None,
        "cpu_peak_pct": max((row["cpu_pct"] for row in active), default=None),
        "cpu_seconds": cpu_seconds,
        "duration_s": duration,
        "samples": len(active),
        "pss_mean_mib": sum(memory) / len(memory) / 2**20 if memory else None,
        "pss_peak_mib": max(memory) / 2**20 if memory else None,
    }
