"""Read and summarize worker process and Pulp temporary-file samples."""

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


def worker_resource_series(samples: list[dict]) -> dict[str, list[tuple]]:
    """Sum API-worker CPU and PSS at each sample timestamp."""
    cpu = defaultdict(float)
    memory = defaultdict(float)
    for row in samples:
        timestamp = row["timestamp_utc"]
        if row.get("cpu_pct") is not None:
            cpu[timestamp] += row["cpu_pct"]
        if row.get("pss_bytes") is not None:
            memory[timestamp] += row["pss_bytes"] / MIB
    return {
        "cpu_pct": sorted(cpu.items()),
        "pss_mib": sorted(memory.items()),
    }


def summarize_worker_resources(samples: list[dict]) -> dict | None:
    """Summarize API-worker CPU time and aggregate proportional memory."""
    series = worker_resource_series(samples)
    cpu = [value for _, value in series["cpu_pct"]]
    memory = [value for _, value in series["pss_mib"]]
    if not cpu and not memory:
        return None
    return {
        "cpu_mean_pct": sum(cpu) / len(cpu) if cpu else None,
        "cpu_peak_pct": max(cpu) if cpu else None,
        "cpu_seconds": sum(row.get("cpu_seconds") or 0 for row in samples),
        "pss_mean_mib": sum(memory) / len(memory) if memory else None,
        "pss_peak_mib": max(memory) if memory else None,
        "samples": len(memory),
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
