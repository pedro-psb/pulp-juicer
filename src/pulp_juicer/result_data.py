"""Normalize Locust CSV exports and run measurements into typed Parquet tables."""

import csv
from datetime import datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.csv as pacsv
import pyarrow.parquet as pq

from .execution_timing import normalize_execution_timings

LOCUST_EXPORTS = (
    "locust_stats",
    "locust_stats_history",
    "locust_failures",
    "locust_exceptions",
)

REQUEST_SCHEMA = pa.schema(
    [
        pa.field("started_at_utc", pa.timestamp("us", tz="UTC")),
        pa.field("completed_at_utc", pa.timestamp("us", tz="UTC")),
        pa.field("artifact_path", pa.string()),
        pa.field("url", pa.string()),
        pa.field("response_time_ms", pa.float64()),
        pa.field("status_code", pa.int32()),
        pa.field("response_bytes", pa.int64()),
        pa.field("success", pa.bool_()),
        pa.field("error", pa.string()),
    ]
)

DATA_LOAD_SCHEMA = pa.schema(
    [
        pa.field("path", pa.string()),
        pa.field("url", pa.string()),
        pa.field("name", pa.string()),
        pa.field("expected_status", pa.int32()),
        pa.field("sha256", pa.string()),
        pa.field("wait_seconds", pa.float64()),
        pa.field("request_timeout_seconds", pa.float64()),
        pa.field("method", pa.string()),
        pa.field("relative_path", pa.string()),
        pa.field("file_path", pa.string()),
        pa.field("repository", pa.string()),
    ]
)

_LOCUST_INTEGER_FIELDS = {
    "Request Count",
    "Failure Count",
    "Timestamp",
    "User Count",
    "Total Request Count",
    "Total Failure Count",
    "Occurrences",
    "Count",
}
_LOCUST_FLOAT_FIELDS = {
    "Median Response Time",
    "Average Response Time",
    "Min Response Time",
    "Max Response Time",
    "Average Content Size",
    "Requests/s",
    "Failures/s",
    "Total Median Response Time",
    "Total Average Response Time",
    "Total Min Response Time",
    "Total Max Response Time",
    "Total Average Content Size",
}


def _locust_csv_table(path: Path) -> pa.Table:
    """Give empty and populated Locust exports the same column types."""
    with path.open(newline="") as source:
        columns = next(csv.reader(source))
    types = {}
    for name in columns:
        if name in _LOCUST_INTEGER_FIELDS:
            types[name] = pa.int64()
        elif name in _LOCUST_FLOAT_FIELDS or name.endswith("%"):
            types[name] = pa.float64()
        else:
            types[name] = pa.string()
    return pacsv.read_csv(
        path, convert_options=pacsv.ConvertOptions(column_types=types)
    )


def read_rows(run_dir: Path, stem: str) -> list[dict]:
    """Read normalized run data without parsing CSV in analysis code."""
    path = run_dir / f"{stem}.parquet"
    return pq.read_table(path).to_pylist() if path.is_file() else []


def graph_path(directory: Path, name: str) -> Path:
    output = directory / "graphs"
    output.mkdir(exist_ok=True)
    return output / name


def _legacy_requests(path: Path) -> pa.Table:
    with path.open(newline="") as source:
        rows = list(csv.DictReader(source))
    normalized = []
    for row in rows:
        normalized.append(
            {
                "started_at_utc": datetime.fromisoformat(row["started_at_utc"]),
                "completed_at_utc": datetime.fromisoformat(
                    row.get("completed_at_utc") or row["started_at_utc"]
                ),
                "artifact_path": row.get("artifact_path", ""),
                "url": row.get("url", ""),
                "response_time_ms": float(row["response_time_ms"])
                if row.get("response_time_ms")
                else None,
                "status_code": int(row["status_code"])
                if row.get("status_code")
                else None,
                "response_bytes": int(row["response_bytes"])
                if row.get("response_bytes")
                else None,
                "success": row.get("success", "").lower() == "true",
                "error": row.get("error", ""),
            }
        )
    return pa.Table.from_pylist(normalized, schema=REQUEST_SCHEMA)


def _legacy_data_load(path: Path) -> pa.Table:
    with path.open(newline="") as source:
        rows = list(csv.DictReader(source))
    for row in rows:
        for name in ("expected_status",):
            row[name] = int(row[name]) if row.get(name) else None
        for name in ("wait_seconds", "request_timeout_seconds"):
            row[name] = float(row[name]) if row.get(name) else None
    return pa.Table.from_pylist(rows, schema=DATA_LOAD_SCHEMA)


def normalize_run(run_dir: Path) -> None:
    """Convert Locust exports, and migrate older CSV results, before analysis."""
    for old_graph in run_dir.glob("*.svg"):
        old_graph.replace(graph_path(run_dir, old_graph.name))
    raw = run_dir / "raw"
    for stem in LOCUST_EXPORTS:
        legacy = run_dir / f"{stem}.csv"
        source = raw / legacy.name
        if legacy.is_file():
            raw.mkdir(exist_ok=True)
            legacy.replace(source)
        if source.is_file():
            destination = run_dir / f"{stem}.parquet"
            if (
                not destination.is_file()
                or source.stat().st_mtime_ns > destination.stat().st_mtime_ns
            ):
                pq.write_table(
                    _locust_csv_table(source), destination, compression="zstd"
                )
    for stem, converter in (
        ("requests", _legacy_requests),
        ("data_load", _legacy_data_load),
    ):
        legacy = run_dir / f"{stem}.csv"
        if legacy.is_file():
            destination = run_dir / f"{stem}.parquet"
            if (
                not destination.is_file()
                or legacy.stat().st_mtime_ns > destination.stat().st_mtime_ns
            ):
                pq.write_table(converter(legacy), destination, compression="zstd")
            raw.mkdir(exist_ok=True)
            legacy.replace(raw / legacy.name)


def normalize_execution(execution_dir: Path) -> None:
    """Normalize all run directories before report cache checks."""
    for old_graph in execution_dir.glob("*.svg"):
        old_graph.replace(graph_path(execution_dir, old_graph.name))
    normalize_execution_timings(execution_dir)
    for run_dir in execution_dir.glob("*/*/*"):
        if run_dir.is_dir():
            normalize_run(run_dir)
