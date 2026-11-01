"""Read and summarize content-worker I/O and Pulp temporary-file samples."""

from collections import defaultdict
from pathlib import Path

import pyarrow.parquet as pq

MIB = 2**20


def read_samples(run_dir: Path, collector: str) -> list[dict]:
    """Return one collector's rows, or no rows for older runs without it."""
    path = run_dir / "collectors" / f"{collector}.parquet"
    return pq.read_table(path).to_pylist() if path.is_file() else []


def content_io_series(samples: list[dict]) -> dict[str, list[tuple]]:
    """Sum worker rates at each timestamp to show whole-content-app activity."""
    totals = defaultdict(lambda: [0.0, 0.0])
    for row in samples:
        if row.get("storage_read_bytes_per_s") is None:
            continue
        values = totals[row["timestamp_utc"]]
        values[0] += row["storage_read_bytes_per_s"] / MIB
        values[1] += (row.get("storage_write_bytes_per_s") or 0) / MIB
    times = sorted(totals)
    return {
        "read_mib_s": [(time, totals[time][0]) for time in times],
        "write_mib_s": [(time, totals[time][1]) for time in times],
    }


def summarize_content_io(samples: list[dict]) -> dict | None:
    """Estimate observed byte totals from rates and per-worker intervals."""
    active = [
        row
        for row in samples
        if row.get("interval_s") and row.get("storage_read_bytes_per_s") is not None
    ]
    if not active:
        return None
    series = content_io_series(active)
    return {
        "read_mib": sum(
            row["storage_read_bytes_per_s"] * row["interval_s"] for row in active
        )
        / MIB,
        "write_mib": sum(
            (row.get("storage_write_bytes_per_s") or 0) * row["interval_s"]
            for row in active
        )
        / MIB,
        "peak_read_mib_s": max(value for _, value in series["read_mib_s"]),
        "peak_write_mib_s": max(value for _, value in series["write_mib_s"]),
        "read_syscalls": round(
            sum(
                (row.get("read_syscalls_per_s") or 0) * row["interval_s"]
                for row in active
            )
        ),
        "write_syscalls": round(
            sum(
                (row.get("write_syscalls_per_s") or 0) * row["interval_s"]
                for row in active
            )
        ),
    }


def summarize_pulp_temp(samples: list[dict]) -> dict | None:
    """Report sampled peaks from the shared Pulp working directory."""
    if not samples:
        return None
    return {
        "peak_files": max(row["active_files"] for row in samples),
        "peak_mib": max(row["active_bytes"] for row in samples) / MIB,
        "peak_root_mib": max(row["root_bytes"] for row in samples) / MIB,
        "peak_allocated_mib": max(row["allocated_bytes"] for row in samples) / MIB,
    }
