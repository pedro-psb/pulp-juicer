"""Extract Pulp task lifecycle records from Compose logs for reporting."""

import html
import json
import math
import re
import statistics
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

TASK_SCHEMA = pa.schema(
    [
        pa.field("task_id", pa.string(), nullable=False),
        pa.field("task_type", pa.string()),
        pa.field("domain", pa.string()),
        pa.field("service", pa.string()),
        pa.field("worker", pa.string()),
        pa.field("immediate", pa.bool_()),
        pa.field("deferred", pa.bool_()),
        pa.field("dispatched_at_utc", pa.timestamp("us", tz="UTC")),
        pa.field("started_at_utc", pa.timestamp("us", tz="UTC")),
        pa.field("finished_at_utc", pa.timestamp("us", tz="UTC")),
        pa.field("status", pa.string(), nullable=False),
        pa.field("execution_time_us", pa.int64()),
        pa.field("observed_elapsed_us", pa.int64()),
        pa.field("waiting_time_us", pa.int64()),
        pa.field("failure_reason", pa.string()),
    ],
    metadata={
        b"source": b"compose.log",
        b"duration_unit": b"microseconds",
        b"version": b"2",
    },
)

_PREFIX = re.compile(r"^\s*(?P<service>[^|]+)\|\s*(?P<timestamp>\S+)\s+")
_LOGGER = re.compile(r"pulpcore\.tasking\.tasks:(?:INFO|WARNING|ERROR):\s*")
_DISPATCH = re.compile(
    r"pulpcore\.tasking\._util:INFO:\s*Dispatched scheduled task "
    r"(?P<task_type>\S+) as task id (?P<task_id>\S+) in domain (?P<domain>\S+)"
)
_START = re.compile(
    r"^Starting task id: (?P<task_id>\S+) in domain: (?P<domain>[^,]+), "
    r"task_type: (?P<task_type>[^,]+), immediate: (?P<immediate>True|False), "
    r"deferred: (?P<deferred>True|False), worker: (?P<worker>\S+)"
)
_COMPLETE = re.compile(
    r"^Task completed (?P<task_id>\S+) in domain: (?P<domain>[^,]+), "
    r"task_type: (?P<task_type>[^,]+), immediate: (?P<immediate>True|False), "
    r"deferred: (?P<deferred>True|False), execution_time: "
    r"(?P<execution_time_us>\d+) (?:μs|us)"
)
_FAIL = re.compile(
    r"^Task\[(?P<task_type>[^]]+)\] (?P<task_id>\S+) failed "
    r"\((?P<failure_reason>.*)\) in domain: (?P<domain>.+)$"
)


def parse_tasking_log(log_path: Path) -> list[dict]:
    """Join start and terminal log records by task ID, retaining partial records."""
    records: dict[str, dict] = {}
    dispatches: dict[str, datetime] = {}
    with log_path.open(errors="replace") as source:
        for line in source:
            prefix = _PREFIX.match(line)
            if prefix is None:
                continue
            try:
                timestamp = datetime.fromisoformat(prefix["timestamp"]).astimezone(UTC)
            except ValueError:
                continue
            dispatch = _DISPATCH.search(line, prefix.end())
            if dispatch:
                task_id = dispatch["task_id"]
                dispatches[task_id] = timestamp
                continue
            marker = _LOGGER.search(line, prefix.end())
            if marker is None:
                continue
            message = line[marker.end() :].strip()
            start = _START.match(message)
            complete = _COMPLETE.match(message)
            failure = _FAIL.match(message)
            event = start or complete or failure
            if event is None:
                continue
            fields = event.groupdict()
            task_id = fields.pop("task_id")
            row = records.setdefault(
                task_id,
                {
                    "task_id": task_id,
                    "task_type": None,
                    "domain": None,
                    "service": None,
                    "worker": None,
                    "immediate": None,
                    "deferred": None,
                    "dispatched_at_utc": None,
                    "started_at_utc": None,
                    "finished_at_utc": None,
                    "status": "started",
                    "execution_time_us": None,
                    "observed_elapsed_us": None,
                    "waiting_time_us": None,
                    "failure_reason": None,
                },
            )
            row["service"] = prefix["service"].strip()
            row["task_type"] = fields["task_type"]
            row["domain"] = fields["domain"]
            for flag in ("immediate", "deferred"):
                if flag in fields:
                    row[flag] = fields[flag] == "True"
            if start:
                row["started_at_utc"] = timestamp
                row["worker"] = fields["worker"]
            elif complete:
                row["finished_at_utc"] = timestamp
                row["status"] = "completed"
                row["execution_time_us"] = int(fields["execution_time_us"])
            else:
                row["finished_at_utc"] = timestamp
                row["status"] = "failed"
                row["failure_reason"] = fields["failure_reason"]
    for row in records.values():
        dispatched = dispatches.get(row["task_id"])
        if dispatched:
            row["dispatched_at_utc"] = dispatched
            if row["started_at_utc"]:
                row["waiting_time_us"] = max(
                    0,
                    int(
                        (row["started_at_utc"] - dispatched).total_seconds() * 1_000_000
                    ),
                )
        if row["started_at_utc"] and row["finished_at_utc"]:
            row["observed_elapsed_us"] = max(
                0,
                int(
                    (row["finished_at_utc"] - row["started_at_utc"]).total_seconds()
                    * 1_000_000
                ),
            )
    return sorted(
        records.values(),
        key=lambda row: (
            row["started_at_utc"] or row["finished_at_utc"],
            row["task_id"],
        ),
    )


def analyze_compose_log(run_dir: Path) -> tuple[Path | None, bool]:
    """Write one task-level Parquet file when the Compose log is new or changed."""
    log_path = run_dir / "compose.log"
    if not log_path.is_file():
        return None, False
    output = run_dir / "tasking.parquet"
    if (
        output.is_file()
        and output.stat().st_mtime_ns >= log_path.stat().st_mtime_ns
        and pq.read_schema(output) == TASK_SCHEMA
    ):
        return output, False
    table = pa.Table.from_pylist(parse_tasking_log(log_path), schema=TASK_SCHEMA)
    temporary = output.with_suffix(".parquet.tmp")
    try:
        pq.write_table(table, temporary, compression="zstd")
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    return output, True


def analyze_execution_tasking(execution_dir: Path) -> bool:
    """Analyze every unit log, including logs from incomplete runs."""
    changed = False
    for log_path in sorted(execution_dir.glob("*/*/*/compose.log")):
        if any(
            part.startswith("_") for part in log_path.relative_to(execution_dir).parts
        ):
            continue
        _, updated = analyze_compose_log(log_path.parent)
        changed |= updated
    return changed


def task_metrics(rows: list[dict]) -> dict:
    """Summarize logged task events over their observed time window."""
    statuses = Counter(row["status"] for row in rows)
    starts = [row["started_at_utc"] for row in rows if row["started_at_utc"]]
    finishes = [row["finished_at_utc"] for row in rows if row["finished_at_utc"]]
    events = [*starts, *finishes]
    duration_s = (max(events) - min(events)).total_seconds() if events else 0
    waits = [
        row["waiting_time_us"] / 1000
        for row in rows
        if row.get("waiting_time_us") is not None
    ]
    durations = [
        row["execution_time_us"] / 1000
        for row in rows
        if row["execution_time_us"] is not None
    ]
    return {
        "tasks": len(rows),
        "started": len(starts),
        "completed": statuses["completed"],
        "failed": statuses["failed"],
        "task_types": len({row["task_type"] for row in rows if row["task_type"]}),
        "workers": len({row["worker"] for row in rows if row["worker"]}),
        "throughput": statuses["completed"] / duration_s if duration_s > 0 else None,
        "window_s": duration_s if duration_s > 0 else None,
        "median_execution_ms": statistics.median(durations) if durations else None,
        "max_execution_ms": max(durations) if durations else None,
        "sum_execution_s": sum(durations) / 1000,
        "wait_count": len(waits),
        "median_wait_ms": statistics.median(waits) if waits else None,
        "max_wait_ms": max(waits) if waits else None,
    }


def wait_distribution_chart(
    path: Path, traces: list[tuple[str, str, list[float], int]]
) -> bool:
    """Plot task wait counts in shared, rounded millisecond buckets."""
    traces = [trace for trace in traces if trace[2]]
    if not traces:
        return False
    width, left, right, top, bottom = 900, 76, 24, 32, 290
    legend_top = bottom + 66
    height = legend_top + len(traces) * 22 + 18
    plot_width = width - left - right
    plot_height = bottom - top
    maximum_wait = max(value for _, _, values, _ in traces for value in values)
    target_bins = max(
        2,
        min(
            10, math.ceil(2 * math.sqrt(max(len(values) for _, _, values, _ in traces)))
        ),
    )
    raw_width = max(maximum_wait, 1) / target_bins
    scale = 10 ** math.floor(math.log10(raw_width))
    bin_width = next(
        multiplier * scale
        for multiplier in (1, 2, 5, 10)
        if multiplier * scale >= raw_width
    )
    bin_count = max(1, math.ceil(maximum_wait / bin_width))
    slot_width = plot_width / bin_count
    histograms = []
    for name, color, values, total_tasks in traces:
        counts = Counter(min(bin_count - 1, int(value / bin_width)) for value in values)
        histograms.append((name, color, values, total_tasks, counts))
    maximum_count = max(
        count for _, _, _, _, counts in histograms for count in counts.values()
    )
    y_scale = maximum_count * 1.08

    def y(count):
        return bottom - count * plot_height / y_scale

    parts = [
        (
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
            'role="img" aria-label="Dispatch-to-start wait distribution">'
        ),
        '<rect width="100%" height="100%" fill="#fff"/>',
    ]
    tick_step = max(1, math.ceil(maximum_count / 4))
    ticks = list(range(0, maximum_count + 1, tick_step))
    if ticks[-1] != maximum_count:
        ticks.append(maximum_count)
    for count in ticks:
        tick_y = y(count)
        parts += [
            f'<line x1="{left}" y1="{tick_y:.1f}" x2="{width - right}" y2="{tick_y:.1f}" stroke="#cbd5e1"/>',
            f'<text x="{left - 8}" y="{tick_y + 4:.1f}" text-anchor="end">{count}</text>',
        ]
    for edge in range(bin_count + 1):
        x = left + edge * slot_width
        parts.append(
            f'<line x1="{x:.1f}" y1="{top}" x2="{x:.1f}" y2="{bottom}" stroke="#cbd5e1" stroke-dasharray="3 3"/>'
        )
    for bucket in range(bin_count):
        center = left + (bucket + 0.5) * slot_width
        lower = bucket * bin_width
        upper = (bucket + 1) * bin_width
        parts.append(
            f'<text x="{center:.1f}" y="{bottom + 20}" text-anchor="middle" font-size="11">'
            f"{lower:.3g}–{upper:.3g}</text>"
        )
    for index, (name, color, values, total_tasks, counts) in enumerate(histograms):
        group_width = slot_width * 0.8
        bar_width = group_width / len(histograms)
        for bucket, count in counts.items():
            x = (
                left
                + bucket * slot_width
                + (slot_width - group_width) / 2
                + index * bar_width
            )
            bar_top = y(count)
            parts.append(
                f'<rect x="{x:.1f}" y="{bar_top:.1f}" width="{bar_width * 0.9:.1f}" '
                f'height="{bottom - bar_top:.1f}" fill="{color}" data-count="{count}" data-bucket="{bucket}"/>'
            )
            if bar_width >= 18:
                parts.append(
                    f'<text x="{x + bar_width * 0.45:.1f}" y="{bar_top - 5:.1f}" '
                    f'text-anchor="middle" font-size="11">{count}</text>'
                )
        legend_y = legend_top + index * 22
        parts += [
            f'<rect x="{left}" y="{legend_y - 6}" width="12" height="12" fill="{color}"/>',
            f'<text x="{left + 20}" y="{legend_y + 4}">{html.escape(name)}: {len(values)} of {total_tasks} tasks have wait data</text>',
        ]
    parts += [
        f'<line x1="{left}" y1="{top}" x2="{left}" y2="{bottom}" stroke="#94a3b8"/>',
        f'<line x1="{left}" y1="{bottom}" x2="{width - right}" y2="{bottom}" stroke="#94a3b8"/>',
        f'<text x="{width / 2}" y="{bottom + 45}" text-anchor="middle">Dispatch-to-start wait (ms)</text>',
        f'<text x="20" y="{(top + bottom) / 2}" transform="rotate(-90 20 {(top + bottom) / 2})" text-anchor="middle">Tasks with wait data (count)</text>',
        "</svg>",
    ]
    path.write_text("\n".join(parts) + "\n")
    return True


def _task_chart(path: Path, rows: list[dict], load_start: datetime | None) -> bool:
    started = [row["started_at_utc"] for row in rows if row["started_at_utc"]]
    completed = [
        row["finished_at_utc"]
        for row in rows
        if row["status"] == "completed" and row["finished_at_utc"]
    ]
    events = [*started, *completed]
    if not events:
        return False
    origin = load_start or min(events)
    start_counts = Counter(
        math.floor((event - origin).total_seconds()) for event in started
    )
    complete_counts = Counter(
        math.floor((event - origin).total_seconds()) for event in completed
    )
    first = min(0, *start_counts, *complete_counts)
    last = max(1, *start_counts, *complete_counts)
    maximum = max(1, *start_counts.values(), *complete_counts.values())
    width, height = 900, 365
    left, right, top, bottom = 72, 22, 30, 290
    plot_width = width - left - right
    plot_height = bottom - top

    def xy(second, count):
        return (
            left + (second - first) * plot_width / (last - first),
            bottom - count * plot_height / maximum,
        )

    parts = [
        (
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
            'role="img" aria-label="Pulp tasks started and completed per second">'
        ),
        '<rect width="100%" height="100%" fill="#fff"/>',
    ]
    for index in range(5):
        count = maximum * index / 4
        y = xy(0, count)[1]
        second = first + (last - first) * index / 4
        x = xy(second, 0)[0]
        parts += [
            f'<line x1="{left}" y1="{y:.1f}" x2="{width - right}" y2="{y:.1f}" stroke="#e5e7eb"/>',
            f'<text x="{left - 7}" y="{y + 4:.1f}" text-anchor="end">{count:.0f}</text>',
            f'<text x="{x:.1f}" y="{bottom + 20}" text-anchor="middle">{second:.0f}</text>',
        ]
    for name, counts, color, legend_x in (
        ("Started", start_counts, "#2563eb", left),
        ("Completed", complete_counts, "#16a34a", left + 160),
    ):
        points = " ".join(
            f"{x:.1f},{y:.1f}"
            for x, y in (
                xy(second, counts[second]) for second in range(first, last + 1)
            )
        )
        parts += [
            f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="2.5"/>',
            f'<line x1="{legend_x}" y1="18" x2="{legend_x + 25}" y2="18" stroke="{color}" stroke-width="3"/>',
            f'<text x="{legend_x + 32}" y="22">{name}</text>',
        ]
    parts += [
        f'<line x1="{left}" y1="{top}" x2="{left}" y2="{bottom}" stroke="#94a3b8"/>',
        f'<line x1="{left}" y1="{bottom}" x2="{width - right}" y2="{bottom}" stroke="#94a3b8"/>',
        f'<text x="{width / 2}" y="{bottom + 46}" text-anchor="middle">'
        + (
            "Time since load start (s)"
            if load_start
            else "Time since first task event (s)"
        )
        + "</text>",
        (
            f'<text x="18" y="{(top + bottom) / 2}" '
            f'transform="rotate(-90 18 {(top + bottom) / 2})" text-anchor="middle">'
            "Tasks per second</text>"
        ),
        "</svg>",
    ]
    path.write_text("\n".join(parts) + "\n")
    return True


def task_report_sections(
    run_dir: Path, execution_dir: Path
) -> tuple[list[str], list[str], list[str]]:
    """Build task summary, distribution, and timeline sections from Parquet."""
    parquet_path = run_dir / "tasking.parquet"
    if not parquet_path.is_file():
        return [], [], []
    rows = pq.read_table(parquet_path).to_pylist()
    asset = run_dir.relative_to(execution_dir).as_posix()
    lines = [
        "#### Pulp tasks",
        "",
        (
            "Task records include setup and background activity from the Compose log. "
            "Execution time is reported by Pulp and excludes queue waiting."
        ),
        "",
    ]
    if not rows:
        lines += ["No Pulp task lifecycle records were found in the Compose log.", ""]
        return lines, [], []
    metrics = task_metrics(rows)
    missing_starts = sum(
        row["status"] == "completed" and row["started_at_utc"] is None for row in rows
    )
    lines += [
        "| Measure | Value |",
        "|:--|--:|",
        f"| Tasks observed | {metrics['tasks']} |",
        f"| Started records | {metrics['started']} |",
        f"| Completed | {metrics['completed']} |",
        f"| Failed | {metrics['failed']} |",
        f"| Completed without start record | {missing_starts} |",
        f"| Task throughput | {metrics['throughput']:,.2f} completed/s |"
        if metrics["throughput"] is not None
        else "| Task throughput | N/A |",
        f"| Observed task window | {metrics['window_s']:,.2f} s |"
        if metrics["window_s"] is not None
        else "| Observed task window | N/A |",
        f"| Task types | {metrics['task_types']} |",
        f"| Workers observed | {metrics['workers']} |",
        f"| Sum of reported execution time | {metrics['sum_execution_s']:,.2f} s |",
        f"| Median completed execution time | {metrics['median_execution_ms']:,.2f} ms |"
        if metrics["median_execution_ms"] is not None
        else "| Median completed execution time | N/A |",
        f"| Tasks with dispatch and start logs | {metrics['wait_count']} |",
        f"| Median dispatch-to-start wait | {metrics['median_wait_ms']:,.2f} ms |"
        if metrics["median_wait_ms"] is not None
        else "| Median dispatch-to-start wait | N/A |",
        f"| Max dispatch-to-start wait | {metrics['max_wait_ms']:,.2f} ms |"
        if metrics["max_wait_ms"] is not None
        else "| Max dispatch-to-start wait | N/A |",
        "",
        "Task throughput is completed tasks divided by the span from the first to last logged task event. Waiting time is available only when the log has a matching dispatch entry, currently for scheduled tasks.",
        "",
    ]
    waits = [
        row["waiting_time_us"] / 1000
        for row in rows
        if row.get("waiting_time_us") is not None
    ]
    distribution = []
    if wait_distribution_chart(
        run_dir / "report_task_wait.svg", [("This run", "#2563eb", waits, len(rows))]
    ):
        distribution = [
            "#### Dispatch-to-start wait distribution",
            "",
            "Bars count tasks with measured waits in each labeled millisecond range. Other tasks have no dispatch timestamp in the log.",
            "",
            f"![Dispatch-to-start wait distribution]({asset}/report_task_wait.svg)",
            "",
        ]
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["task_type"] or "Unknown"].append(row)

    def summary(task_type, group):
        times = [
            row["execution_time_us"] / 1000
            for row in group
            if row["execution_time_us"] is not None
        ]
        duration = f"{max(times):,.2f}" if times else "N/A"
        return (
            f"| {html.escape(task_type).replace('|', '&#124;')} | {len(group)} | "
            f"{sum(row['status'] == 'completed' for row in group)} | "
            f"{sum(row['status'] == 'failed' for row in group)} | {duration} |"
        )

    header = [
        "| Task type | Tasks | Completed | Failed | Max execution (ms) |",
        "|:--|--:|--:|--:|--:|",
    ]
    lines += ["##### Most frequent task types", "", *header]
    for task_type, group in sorted(
        grouped.items(), key=lambda item: (-len(item[1]), item[0])
    )[:10]:
        lines.append(summary(task_type, group))
    lines += ["", "##### Longest execution by task type", "", *header]
    longest = sorted(
        (
            (task_type, group)
            for task_type, group in grouped.items()
            if any(row["execution_time_us"] is not None for row in group)
        ),
        key=lambda item: (
            -max(
                row["execution_time_us"]
                for row in item[1]
                if row["execution_time_us"] is not None
            ),
            item[0],
        ),
    )
    for task_type, group in longest[:10]:
        lines.append(summary(task_type, group))
    if not longest:
        lines.append("No completed task durations were reported.")
    lines.append("")

    metadata_path = run_dir / "run_metadata.json"
    metadata = json.loads(metadata_path.read_text()) if metadata_path.is_file() else {}
    load_start = metadata.get("result", {}).get("load_start_utc")
    origin = datetime.fromisoformat(load_start) if load_start else None
    chart_path = run_dir / "report_tasks.svg"
    timeline = []
    if _task_chart(chart_path, rows, origin):
        timeline = [
            "#### Pulp tasks",
            "",
            f"![Pulp tasks started and completed per second]({asset}/report_tasks.svg)",
            "",
        ]
    return lines, distribution, timeline
