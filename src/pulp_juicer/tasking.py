"""Summarize Pulp task rows captured from the database collector."""

import html
import json
import math
import statistics
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import pyarrow.parquet as pq


def task_metrics(rows: list[dict]) -> dict:
    """Summarize stored task states and their observed execution window."""
    statuses = Counter(row["status"] for row in rows)
    starts = [row["started_at_utc"] for row in rows if row["started_at_utc"]]
    finishes = [row["finished_at_utc"] for row in rows if row["finished_at_utc"]]
    events = [*starts, *finishes]
    duration_s = (max(events) - min(events)).total_seconds() if events else 0

    def delays(field):
        return [row[field] / 1000 for row in rows if row.get(field) is not None]

    waits = delays("queue_wait_us")
    unblock = delays("time_to_unblock_us")
    ready = delays("ready_wait_us")
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
        "throughput": statuses["completed"] / duration_s if duration_s > 0 else None,
        "window_s": duration_s if duration_s > 0 else None,
        "median_execution_ms": statistics.median(durations) if durations else None,
        "total_execution_ms": sum(durations) if durations else None,
        "sum_execution_s": sum(durations) / 1000,
        "wait_count": len(waits),
        "median_wait_ms": statistics.median(waits) if waits else None,
        "max_wait_ms": max(waits) if waits else None,
        "unblock_count": len(unblock),
        "median_unblock_ms": statistics.median(unblock) if unblock else None,
        "ready_count": len(ready),
        "median_ready_ms": statistics.median(ready) if ready else None,
    }


def wait_distribution_chart(
    path: Path,
    traces: list[tuple[str, str, list[float], int]],
    label: str = "Queue wait",
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
            f'role="img" aria-label="{html.escape(label, quote=True)} distribution">'
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
        f'<text x="{width / 2}" y="{bottom + 45}" text-anchor="middle">{html.escape(label)} (ms)</text>',
        f'<text x="20" y="{(top + bottom) / 2}" transform="rotate(-90 20 {(top + bottom) / 2})" text-anchor="middle">Tasks with wait data (count)</text>',
        "</svg>",
    ]
    path.write_text("\n".join(parts) + "\n")
    return True


def _task_chart(path: Path, rows: list[dict], load_start: datetime | None) -> bool:
    dispatched = [row["created_at_utc"] for row in rows if row["created_at_utc"]]
    started = [row["started_at_utc"] for row in rows if row["started_at_utc"]]
    completed = [
        row["finished_at_utc"]
        for row in rows
        if row["status"] == "completed" and row["finished_at_utc"]
    ]
    events = [*dispatched, *started, *completed]
    if not events:
        return False
    origin = load_start or min(events)
    dispatch_counts = Counter(
        math.floor((event - origin).total_seconds()) for event in dispatched
    )
    start_counts = Counter(
        math.floor((event - origin).total_seconds()) for event in started
    )
    complete_counts = Counter(
        math.floor((event - origin).total_seconds()) for event in completed
    )
    first = min(0, *dispatch_counts, *start_counts, *complete_counts)
    last = max(1, *dispatch_counts, *start_counts, *complete_counts)
    maximum = max(
        1, *dispatch_counts.values(), *start_counts.values(), *complete_counts.values()
    )
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
            'role="img" aria-label="Pulp tasks dispatched, started, and completed per second">'
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
        ("Dispatched", dispatch_counts, "#d97706", left),
        ("Started", start_counts, "#2563eb", left + 160),
        ("Completed", complete_counts, "#16a34a", left + 320),
    ):
        points = " ".join(
            f"{x:.1f},{y:.1f}"
            for x, y in (
                xy(second, counts[second]) for second in range(first, last + 1)
            )
        )
        parts += [
            f'<polyline data-series="{name}" points="{points}" fill="none" stroke="{color}" stroke-width="2.5"/>',
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
    parquet_path = run_dir / "collectors" / "tasking.parquet"
    if not parquet_path.is_file():
        return [], [], []
    rows = pq.read_table(parquet_path).to_pylist()
    asset = run_dir.relative_to(execution_dir).as_posix()
    lines = [
        "#### Pulp tasks",
        "",
        (
            "The database snapshot includes setup and background tasks. "
            "Execution time excludes time before a task starts."
        ),
        "",
    ]
    if not rows:
        lines += ["No Pulp tasks were found in the database snapshot.", ""]
        return lines, [], []
    metrics = task_metrics(rows)
    lines += [
        "| Measure | Value |",
        "|:--|--:|",
        f"| Tasks observed | {metrics['tasks']} |",
        f"| Started | {metrics['started']} |",
        f"| Completed | {metrics['completed']} |",
        f"| Failed | {metrics['failed']} |",
        f"| Task throughput | {metrics['throughput']:,.2f} completed/s |"
        if metrics["throughput"] is not None
        else "| Task throughput | N/A |",
        f"| Observed task window | {metrics['window_s']:,.2f} s |"
        if metrics["window_s"] is not None
        else "| Observed task window | N/A |",
        f"| Task types | {metrics['task_types']} |",
        f"| Total execution time | {metrics['sum_execution_s']:,.2f} s |",
        f"| Median execution time | {metrics['median_execution_ms']:,.2f} ms |"
        if metrics["median_execution_ms"] is not None
        else "| Median execution time | N/A |",
        f"| Median total queue wait | {metrics['median_wait_ms']:,.2f} ms |"
        if metrics["median_wait_ms"] is not None
        else "| Median total queue wait | N/A |",
        f"| Median time to unblock | {metrics['median_unblock_ms']:,.2f} ms |"
        if metrics["median_unblock_ms"] is not None
        else "| Median time to unblock | N/A |",
        f"| Median ready queue wait | {metrics['median_ready_ms']:,.2f} ms |"
        if metrics["median_ready_ms"] is not None
        else "| Median ready queue wait | N/A |",
        "",
        "Task throughput is completed tasks divided by the first-to-last task execution event window. Time to unblock runs from creation until Pulp marks a task ready; ready queue wait runs from that point until execution starts. Total queue wait is creation to start. Missing phase timestamps are excluded from those measures.",
        "",
    ]
    distribution = []
    for field, title, color, filename in (
        ("time_to_unblock_us", "Time to unblock", "#2563eb", "report_task_unblock.svg"),
        ("ready_wait_us", "Ready queue wait", "#e56b35", "report_task_ready_wait.svg"),
    ):
        values = [row[field] / 1000 for row in rows if row[field] is not None]
        if wait_distribution_chart(
            run_dir / filename, [("This run", color, values, len(rows))], title
        ):
            distribution += [
                f"#### {title} distribution",
                "",
                "Bars count tasks in shared millisecond ranges; tasks missing either phase timestamp are excluded.",
                "",
                f"![{title} distribution]({asset}/{filename})",
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
        duration = f"{sum(times):,.2f}" if times else "N/A"
        return (
            f"| {html.escape(task_type).replace('|', '&#124;')} | {len(group)} | "
            f"{sum(row['status'] == 'completed' for row in group)} | "
            f"{sum(row['status'] == 'failed' for row in group)} | {duration} |"
        )

    header = [
        "| Task type | Tasks | Completed | Failed | Total execution (ms) |",
        "|:--|--:|--:|--:|--:|",
    ]
    lines += ["##### Most frequent task types", "", *header]
    for task_type, group in sorted(
        grouped.items(), key=lambda item: (-len(item[1]), item[0])
    )[:10]:
        lines.append(summary(task_type, group))
    lines += [
        "",
        "##### Total execution time by task type",
        "",
        "Totals sum the reported execution time of all instances of each task type.",
        "",
        "| Task type | Tasks | Completed | Failed | Total execution (ms) |",
        "|:--|--:|--:|--:|--:|",
    ]
    by_total = sorted(
        (
            (task_type, group)
            for task_type, group in grouped.items()
            if any(row["execution_time_us"] is not None for row in group)
        ),
        key=lambda item: (
            -sum(
                row["execution_time_us"]
                for row in item[1]
                if row["execution_time_us"] is not None
            ),
            item[0],
        ),
    )
    for task_type, group in by_total[:10]:
        lines.append(summary(task_type, group))
    if not by_total:
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
            f"![Pulp tasks dispatched, started, and completed per second]({asset}/report_tasks.svg)",
            "",
        ]
    return lines, distribution, timeline
