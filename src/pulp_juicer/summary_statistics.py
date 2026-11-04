"""Observation-level and equal-run-weight summaries for the interactive report."""

import math
import statistics
from collections import Counter, defaultdict

from .collector_metrics import MIB, read_samples
from .execution_timing import PHASES
from .postgres_metrics import read_postgres_samples
from .result_data import read_rows

_DIST = ("n", "mean", "sd", "median", "p95", "p99", "max")
_TOTAL = (*_DIST, "total")
_COUNT = ("total",)

# A row is a random variable; the unit names the thing sampled, not the unit of measure.
VARIABLES = (
    ("Requests", "request_latency", "Response time", "request", "ms", _DIST),
    ("Requests", "response_bytes", "Response bytes", "request", "bytes", _TOTAL),
    (
        "Requests",
        "request_failure",
        "Request failure",
        "request",
        "%",
        ("n", "total", "percent"),
    ),
    (
        "Requests",
        "request_starts",
        "New requests",
        "one-second bin",
        "requests/s",
        _TOTAL,
    ),
    (
        "Requests",
        "request_completions",
        "Completed requests",
        "one-second bin",
        "requests/s",
        _TOTAL,
    ),
    (
        "Requests",
        "active_users",
        "Active users",
        "Locust history sample",
        "users",
        _DIST,
    ),
    ("Tasks", "task_queue_wait", "Total queue wait", "task", "ms", _DIST),
    ("Tasks", "task_unblock_wait", "Time to unblock", "task", "ms", _DIST),
    ("Tasks", "task_ready_wait", "Ready queue wait", "task", "ms", _DIST),
    ("Tasks", "task_execution", "Execution time", "task", "ms", _TOTAL),
    ("Tasks", "task_status", "Task outcomes", "task", "count / %", ("counts",)),
    ("Tasks", "task_type", "Task types", "task", "count / %", ("counts",)),
    (
        "Tasks",
        "task_dispatches",
        "Tasks dispatched",
        "one-second bin",
        "tasks/s",
        _TOTAL,
    ),
    ("Tasks", "task_starts", "Tasks started", "one-second bin", "tasks/s", _TOTAL),
    (
        "Tasks",
        "task_completions",
        "Tasks completed",
        "one-second bin",
        "tasks/s",
        _TOTAL,
    ),
    ("PostgreSQL", "pg_cpu", "CPU", "collector interval", "% of core", _DIST),
    ("PostgreSQL", "pg_pss", "PSS memory", "collector interval", "MiB", _DIST),
    (
        "PostgreSQL",
        "pg_processes",
        "Process count",
        "collector interval",
        "count",
        _DIST,
    ),
    ("PostgreSQL", "pg_cpu_seconds", "CPU time", "run", "core-seconds", _COUNT),
    (
        "Content storage",
        "content_read",
        "Read rate",
        "collector interval",
        "MiB/s",
        _TOTAL,
    ),
    (
        "Content storage",
        "content_write",
        "Write rate",
        "collector interval",
        "MiB/s",
        _TOTAL,
    ),
    (
        "Content storage",
        "content_read_calls",
        "Read syscalls",
        "collector interval",
        "calls/s",
        _TOTAL,
    ),
    (
        "Content storage",
        "content_write_calls",
        "Write syscalls",
        "collector interval",
        "calls/s",
        _TOTAL,
    ),
    ("API workers", "api_cpu", "CPU", "collector interval", "% of core", _DIST),
    ("API workers", "api_pss", "PSS memory", "collector interval", "MiB", _DIST),
    (
        "API workers",
        "api_processes",
        "Process count",
        "collector interval",
        "count",
        _DIST,
    ),
    ("API workers", "api_cpu_seconds", "CPU time", "run", "core-seconds", _COUNT),
    ("API workers", "api_read", "Read rate", "collector interval", "MiB/s", _TOTAL),
    ("API workers", "api_write", "Write rate", "collector interval", "MiB/s", _TOTAL),
    (
        "API workers",
        "api_read_calls",
        "Read syscalls",
        "collector interval",
        "calls/s",
        _TOTAL,
    ),
    (
        "API workers",
        "api_write_calls",
        "Write syscalls",
        "collector interval",
        "calls/s",
        _TOTAL,
    ),
    (
        "Temporary files",
        "temp_files",
        "Active files",
        "collector interval",
        "count",
        _DIST,
    ),
    (
        "Temporary files",
        "temp_size",
        "Apparent size",
        "collector interval",
        "MiB",
        _DIST,
    ),
    (
        "Temporary files",
        "temp_allocated",
        "Allocated size",
        "collector interval",
        "MiB",
        _DIST,
    ),
    ("Temporary files", "temp_root", "Root size", "collector interval", "MiB", _DIST),
    *(
        ("Trial phases", f"phase_{index}", title, "run", "s", _COUNT)
        for index, title in enumerate(
            ("Total duration", *(phase for phase, _, _ in PHASES))
        )
    ),
)


def _quantile(values, weights, fraction):
    """Linear interpolation for events; inverse-CDF quantile for time occupancy."""
    if weights is not None:
        pairs = sorted(zip(values, weights, strict=True))
        threshold = sum(weights) * fraction
        cumulative = 0.0
        for value, weight in pairs:
            cumulative += weight
            if cumulative >= threshold:
                return value
        return pairs[-1][0]
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    return ordered[lower] + (ordered[math.ceil(position)] - ordered[lower]) * (
        position - lower
    )


def distribution(values, weights=None, total=None):
    """Within-run SD is population SD; weighted observations describe time occupancy."""
    pairs = [
        (float(value), float(weight))
        for value, weight in zip(values, weights or [1] * len(values), strict=True)
        if value is not None
        and math.isfinite(value)
        and weight > 0
        and math.isfinite(weight)
    ]
    if not pairs:
        return None
    numbers, durations = map(list, zip(*pairs, strict=True))
    weight_sum = sum(durations)
    mean = sum(value * weight for value, weight in pairs) / weight_sum
    result = {
        "n": len(numbers),
        "mean": mean,
        "sd": math.sqrt(
            sum(weight * (value - mean) ** 2 for value, weight in pairs) / weight_sum
        ),
        "median": _quantile(numbers, durations if weights is not None else None, 0.5),
        "p95": _quantile(numbers, durations if weights is not None else None, 0.95),
        "p99": _quantile(numbers, durations if weights is not None else None, 0.99),
        "max": max(numbers),
    }
    if total is not None:
        result["total"] = total
    return result


def _window_weight(timestamp, interval_s, markers):
    if not interval_s or interval_s <= 0:
        return 0.0
    if markers is None:
        return interval_s
    start, end = markers
    return max(0.0, min(timestamp, end) - max(timestamp - interval_s, start))


def _resource(samples, fields, markers):
    """Aggregate worker rows by timestamp, then weight each observed interval."""
    grouped = defaultdict(lambda: defaultdict(float))
    intervals = defaultdict(list)
    for row in samples:
        timestamp = row.get("timestamp_utc")
        if timestamp is None:
            continue
        timestamp = timestamp.timestamp()
        interval = row.get("interval_s")
        if interval is not None and interval > 0:
            intervals[timestamp].append(interval)
        for name, source, scale in fields:
            value = row.get(source)
            if value is not None:
                grouped[timestamp][name] += value / scale
    result = {}
    for name, _, _ in fields:
        values, weights = [], []
        for timestamp, measurements in sorted(grouped.items()):
            if name not in measurements:
                continue
            interval = max(intervals[timestamp], default=0)
            weight = _window_weight(timestamp, interval, markers)
            if weight:
                values.append(measurements[name])
                weights.append(weight)
        result[name] = (values, weights)
    return result


def _event_bins(events, start, end):
    if start is None or end is None:
        return []
    counts = Counter(
        math.floor(event.timestamp()) for event in events if event is not None
    )
    return [counts[second] for second in range(math.floor(start), math.floor(end) + 1)]


def summarize_run(trial, markers, timing):
    """Return summaries keyed by the declared variables, omitting unavailable values."""
    rows = read_rows(trial.directory, "requests")
    tasks = trial.tasks or []
    window = (markers["start_abs"], markers["end_abs"]) if markers else None
    result = {}

    def add(key, values, weights=None, total=None):
        summary = distribution(values, weights, total)
        if summary is not None:
            result[key] = summary

    add("request_latency", [row.get("response_time_ms") for row in rows])
    bytes_values = [row.get("response_bytes") for row in rows]
    add(
        "response_bytes",
        bytes_values,
        total=sum(value for value in bytes_values if value is not None),
    )
    if rows and any(row.get("status_code") is not None for row in rows):
        failures = sum(row.get("success") is False for row in rows)
        result["request_failure"] = {
            "n": len(rows),
            "total": failures,
            "percent": failures / len(rows) * 100,
        }
    elif trial.requests:
        result["request_failure"] = {
            "n": trial.requests,
            "total": trial.failures,
            "percent": trial.failures / trial.requests * 100,
        }
    if window:
        for key, field in (
            ("request_starts", "started_at_utc"),
            ("request_completions", "completed_at_utc"),
        ):
            events = [row[field] for row in rows if row.get(field)]
            bins = _event_bins(events, *window)
            add(key, bins, total=len(events))
    history = read_rows(trial.directory, "locust_stats_history")
    add(
        "active_users",
        [row.get("User Count") for row in history if row.get("Name") == "Aggregated"],
    )
    for key, field in (
        ("task_queue_wait", "queue_wait_us"),
        ("task_unblock_wait", "time_to_unblock_us"),
        ("task_ready_wait", "ready_wait_us"),
        ("task_execution", "execution_time_us"),
    ):
        values = [row[field] / 1000 for row in tasks if row.get(field) is not None]
        add(key, values, total=sum(values) if key == "task_execution" else None)
    if tasks:
        for key, field in (("task_status", "status"), ("task_type", "task_type")):
            counts = Counter(row[field] for row in tasks if row.get(field))
            result[key] = {
                "counts": dict(sorted(counts.items())),
                "percentages": {
                    category: count / sum(counts.values()) * 100
                    for category, count in sorted(counts.items())
                },
            }
        task_times = [
            row[field].timestamp()
            for row in tasks
            for field in ("created_at_utc", "started_at_utc", "finished_at_utc")
            if row.get(field)
        ]
        if task_times:
            first, last = min(task_times), max(task_times)
            for key, field, status in (
                ("task_dispatches", "created_at_utc", None),
                ("task_starts", "started_at_utc", None),
                ("task_completions", "finished_at_utc", "completed"),
            ):
                events = [
                    row[field]
                    for row in tasks
                    if row.get(field)
                    and (status is None or row.get("status") == status)
                ]
                add(key, _event_bins(events, first, last), total=len(events))
    resource_specs = (
        (
            "pg",
            read_postgres_samples(trial.directory),
            (
                ("cpu", "cpu_pct", 1),
                ("pss", "pss_bytes", MIB),
                ("processes", "processes", 1),
            ),
        ),
        (
            "content",
            read_samples(trial.directory, "content_io"),
            (
                ("read", "storage_read_bytes_per_s", MIB),
                ("write", "storage_write_bytes_per_s", MIB),
                ("read_calls", "read_syscalls_per_s", 1),
                ("write_calls", "write_syscalls_per_s", 1),
            ),
        ),
        (
            "api",
            read_samples(trial.directory, "api_worker"),
            (
                ("cpu", "cpu_pct", 1),
                ("pss", "pss_bytes", MIB),
                ("read", "storage_read_bytes_per_s", MIB),
                ("write", "storage_write_bytes_per_s", MIB),
                ("read_calls", "read_syscalls_per_s", 1),
                ("write_calls", "write_syscalls_per_s", 1),
            ),
        ),
    )
    for prefix, samples, fields in resource_specs:
        measured = _resource(samples, fields, window)
        for name, source, scale in fields:
            values, weights = measured[name]
            total = None
            if name in ("read", "write", "read_calls", "write_calls"):
                total = sum(
                    row[source]
                    / scale
                    * _window_weight(
                        row["timestamp_utc"].timestamp(), row.get("interval_s"), window
                    )
                    for row in samples
                    if row.get(source) is not None and row.get("timestamp_utc")
                )
            add(
                f"{prefix}_{name}",
                values,
                weights,
                total,
            )
        if prefix in ("pg", "api"):
            seconds = sum(
                (row.get("cpu_seconds") or 0)
                * (
                    _window_weight(
                        row["timestamp_utc"].timestamp(), row.get("interval_s"), window
                    )
                    / row["interval_s"]
                )
                for row in samples
                if row.get("timestamp_utc") and row.get("interval_s")
            )
            if any(row.get("cpu_seconds") is not None for row in samples):
                result[f"{prefix}_cpu_seconds"] = {"total": seconds}
        if prefix == "api":
            counts = defaultdict(set)
            intervals = defaultdict(list)
            for row in samples:
                if row.get("worker_pid") is not None and row.get("timestamp_utc"):
                    counts[row["timestamp_utc"]].add(row["worker_pid"])
                    intervals[row["timestamp_utc"]].append(row.get("interval_s") or 0)
            values, weights = [], []
            for timestamp, pids in sorted(counts.items()):
                interval = max(intervals[timestamp], default=0)
                weight = _window_weight(timestamp.timestamp(), interval, window)
                if weight:
                    values.append(len(pids))
                    weights.append(weight)
            add("api_processes", values, weights)
    temp = read_samples(trial.directory, "pulp_temp")
    temp_fields = (
        ("temp_files", "active_files", 1),
        ("temp_size", "active_bytes", MIB),
        ("temp_allocated", "allocated_bytes", MIB),
        ("temp_root", "root_bytes", MIB),
    )
    ordered = sorted(
        (row for row in temp if row.get("timestamp_utc")),
        key=lambda row: row["timestamp_utc"],
    )
    spacing = [
        max(0, (later["timestamp_utc"] - earlier["timestamp_utc"]).total_seconds())
        for earlier, later in zip(ordered, ordered[1:])
    ]
    default_interval = statistics.median(spacing) if spacing else 1.0
    for key, source, scale in temp_fields:
        values, weights = [], []
        for index, row in enumerate(ordered):
            value = row.get(source)
            interval = spacing[index - 1] if index else default_interval
            weight = _window_weight(row["timestamp_utc"].timestamp(), interval, window)
            if value is not None and weight:
                values.append(value / scale)
                weights.append(weight)
        add(key, values, weights)
    if timing is not None:
        for index, value in enumerate(
            (timing.total_s, *(timing.durations_s[phase] for phase, _, _ in PHASES))
        ):
            if value is not None:
                result[f"phase_{index}"] = {"total": value}
    return result


def summarize_candidate(runs):
    """Each valid run contributes one value to every available statistic."""
    result = {}
    for _, key, _, _, _, fields in VARIABLES:
        summaries = [run["statistics"].get(key) for run in runs]
        available = {}
        for field in fields:
            if field == "counts":
                populated = [summary for summary in summaries if summary is not None]
                categories = sorted(
                    {
                        category
                        for summary in populated
                        for category in summary.get("counts", {})
                    }
                )
                if categories:
                    available[field] = {
                        category: {
                            "n": len(populated),
                            "mean": statistics.mean(values),
                            "sd": statistics.stdev(values) if len(values) > 1 else None,
                            "median": statistics.median(values),
                            "min": min(values),
                            "max": max(values),
                            "percent_mean": statistics.mean(percents),
                        }
                        for category in categories
                        for values in [
                            [
                                summary.get("counts", {}).get(category, 0)
                                for summary in populated
                            ]
                        ]
                        for percents in [
                            [
                                summary.get("percentages", {}).get(category, 0)
                                for summary in populated
                            ]
                        ]
                    }
                continue
            values = [
                summary[field] for summary in summaries if summary and field in summary
            ]
            if values:
                available[field] = {
                    "n": len(values),
                    "mean": statistics.mean(values),
                    "sd": statistics.stdev(values) if len(values) > 1 else None,
                    "median": statistics.median(values),
                    "min": min(values),
                    "max": max(values),
                }
        if available:
            result[key] = available
    return result
