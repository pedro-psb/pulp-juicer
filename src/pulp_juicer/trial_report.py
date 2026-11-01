"""Compare runs as trials within each scenario of one execution."""

import html
import itertools
import json
import math
import re
import shutil
import statistics
import subprocess
from bisect import bisect_right
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pyarrow.parquet as pq

from .collector_metrics import (
    MIB,
    content_io_series,
    read_samples,
    summarize_content_io,
    summarize_pulp_temp,
)
from .execution_timing import PHASES, parse_execution_timings
from .postgres_metrics import read_postgres_samples, summarize_postgres
from .report import completed_runs, markdown_value, read_csv
from .tasking import task_metrics, wait_distribution_chart

_COLORS = ("#2563eb", "#e56b35", "#16a34a", "#a855f7", "#dc2626", "#0891b2")
_TRIAL_LAYOUT_MARKER = "<!-- pulp-juicer-trial-layout: 26 -->"
_METRICS = (
    ("completed", "Cumulative completed requests", "Completed requests (count)"),
    ("throughput", "Completed requests per second", "Completed requests/s"),
    ("starts", "New requests per second", "New requests/s"),
    ("cpu", "PostgreSQL CPU", "PostgreSQL CPU (% of one core)"),
    ("memory", "PostgreSQL memory", "PostgreSQL PSS (MiB)"),
    ("content_read", "Content-worker storage read", "Storage read (MiB/s)"),
    ("content_write", "Content-worker storage write", "Storage write (MiB/s)"),
    ("temp_bytes", "Pulp temporary file size", "Active files (MiB)"),
    ("temp_files", "Pulp temporary file count", "Active files (count)"),
    ("task_starts", "Pulp tasks started per second", "Tasks started/s"),
    ("task_completions", "Pulp tasks completed per second", "Tasks completed/s"),
)


@dataclass
class Trial:
    directory: Path
    scenario: str
    candidate: str
    run_id: str
    requests: int
    failures: int
    rate: float | None
    mean_ms: float | None
    p95_ms: float | None
    cpu_pct: float | None
    memory_mib: float | None
    content_io_summary: dict | None
    temp_summary: dict | None
    metadata: dict | None
    series: dict[str, list[tuple[float, float]]]
    tasks: list[dict] | None

    @property
    def failure_pct(self) -> float | None:
        return self.failures / self.requests * 100 if self.requests else None


def _float(row, key):
    try:
        return float(row[key])
    except (KeyError, TypeError, ValueError):
        return None


def _timeline(run_dir: Path, metadata: dict | None):
    request_path = run_dir / "requests.csv"
    rows = read_csv(request_path) if request_path.is_file() else []
    starts = [datetime.fromisoformat(row["started_at_utc"]).timestamp() for row in rows]
    completions = [
        datetime.fromisoformat(row["completed_at_utc"]).timestamp() for row in rows
    ]
    postgres = read_postgres_samples(run_dir)
    content_io = read_samples(run_dir, "content_io")
    temp = read_samples(run_dir, "pulp_temp")
    saved_start = (metadata or {}).get("result", {}).get("load_start_utc")
    observed = [*starts, *completions]
    observed.extend(row["timestamp_utc"].timestamp() for row in postgres)
    observed.extend(row["timestamp_utc"].timestamp() for row in content_io)
    observed.extend(row["timestamp_utc"].timestamp() for row in temp)
    if not saved_start and not observed:
        return {}
    if saved_start:
        origin = datetime.fromisoformat(saved_start).timestamp()
    elif starts:
        origin = min(starts)
    else:
        origin = min(observed)
    series = {}
    if rows:
        start_counts = Counter(max(0, math.floor(t - origin)) for t in starts)
        completed_counts = Counter(max(0, math.floor(t - origin)) for t in completions)
        last_second = max(0, math.ceil(max((*starts, *completions)) - origin))
        cumulative = 0
        completed = []
        throughput = []
        new_requests = []
        for second in range(last_second + 1):
            count = completed_counts[second]
            cumulative += count
            completed.append((second, cumulative))
            throughput.append((second, count))
            new_requests.append((second, start_counts[second]))
        series.update(completed=completed, throughput=throughput, starts=new_requests)
    if postgres:
        series["cpu"] = [
            (
                row["timestamp_utc"].timestamp() - origin,
                row["cpu_pct"],
            )
            for row in postgres
            if row["cpu_pct"] is not None
        ]
        series["memory"] = [
            (
                row["timestamp_utc"].timestamp() - origin,
                row["pss_bytes"] / 2**20,
            )
            for row in postgres
            if row["pss_bytes"] is not None
        ]
    if content_io:
        io_series = content_io_series(content_io)
        for key, name in (
            ("read_mib_s", "content_read"),
            ("write_mib_s", "content_write"),
        ):
            series[name] = [
                (time.timestamp() - origin, value) for time, value in io_series[key]
            ]
    if temp:
        series["temp_bytes"] = [
            (row["timestamp_utc"].timestamp() - origin, row["active_bytes"] / MIB)
            for row in temp
        ]
        series["temp_files"] = [
            (row["timestamp_utc"].timestamp() - origin, row["active_files"])
            for row in temp
        ]
    return series


def _task_series(tasks: list[dict], metadata: dict | None):
    starts = [row["started_at_utc"] for row in tasks if row["started_at_utc"]]
    completions = [
        row["finished_at_utc"]
        for row in tasks
        if row["status"] == "completed" and row["finished_at_utc"]
    ]
    if not starts and not completions:
        return {}
    saved_start = (metadata or {}).get("result", {}).get("load_start_utc")
    origin = (
        datetime.fromisoformat(saved_start)
        if saved_start
        else min([*starts, *completions])
    )
    series = {}
    for name, events in (("task_starts", starts), ("task_completions", completions)):
        if events:
            counts = Counter(
                math.floor((event - origin).total_seconds()) for event in events
            )
            series[name] = [
                (second, counts[second])
                for second in range(min(0, *counts), max(0, *counts) + 1)
            ]
    return series


def _read_trial(run_dir: Path) -> Trial | None:
    rows = read_csv(run_dir / "locust_stats.csv")
    stats = next((row for row in rows if row.get("Name") == "Aggregated"), None)
    if stats is None:
        return None
    metadata_path = run_dir / "run_metadata.json"
    metadata = (
        json.loads(metadata_path.read_text()) if metadata_path.is_file() else None
    )
    result = (metadata or {}).get("result", {})
    if result.get("timed_out") or (
        result.get("expected_requests") is not None
        and result.get("completed_requests", 0) < result["expected_requests"]
    ):
        return None
    postgres_summary = summarize_postgres(read_postgres_samples(run_dir)) or {}
    content_io_summary = summarize_content_io(read_samples(run_dir, "content_io"))
    temp_summary = summarize_pulp_temp(read_samples(run_dir, "pulp_temp"))
    requests = int(stats["Request Count"])
    task_path = run_dir / "collectors" / "tasking.parquet"
    tasks = pq.read_table(task_path).to_pylist() if task_path.is_file() else None
    series = _timeline(run_dir, metadata)
    if tasks:
        series.update(_task_series(tasks, metadata))
    return Trial(
        directory=run_dir,
        scenario=run_dir.parent.parent.name,
        candidate=run_dir.parent.name,
        run_id=run_dir.name,
        requests=requests,
        failures=int(stats["Failure Count"]),
        rate=_float(stats, "Requests/s"),
        mean_ms=_float(stats, "Average Response Time"),
        p95_ms=_float(stats, "95%"),
        cpu_pct=postgres_summary.get("cpu_mean_pct"),
        memory_mib=postgres_summary.get("pss_mean_mib"),
        content_io_summary=content_io_summary,
        temp_summary=temp_summary,
        metadata=metadata,
        series=series,
        tasks=tasks,
    )


def _format(value, digits=1):
    return f"{value:,.{digits}f}" if value is not None else "N/A"


def _mean_sd(values, digits=2):
    values = [value for value in values if value is not None]
    if not values:
        return "N/A"
    sd = statistics.stdev(values) if len(values) > 1 else None
    return (
        f"{_format(statistics.mean(values), digits)}\u00a0±\u00a0{_format(sd, digits)}"
    )


def _contrast(trials, attribute):
    values = [
        value for trial in trials if (value := getattr(trial, attribute)) is not None
    ]
    return statistics.mean(values) if values else None


def _percentile(values, fraction):
    index = (len(values) - 1) * fraction
    low = math.floor(index)
    high = math.ceil(index)
    return values[low] + (values[high] - values[low]) * (index - low)


def _wait_boxplot(path, groups, colors):
    """Draw one box per candidate from each run's total queue-wait median."""
    samples = {}
    for candidate, trials in sorted(groups.items()):
        run_medians = []
        for trial in trials:
            waits = [
                row["queue_wait_us"] / 1000
                for row in trial.tasks or []
                if row.get("queue_wait_us") is not None
            ]
            if waits:
                run_medians.append((trial.run_id, statistics.median(waits), len(waits)))
        if run_medians:
            samples[candidate] = run_medians
    if not samples:
        return False
    width, left, right, top, row_height = 900, 185, 40, 32, 72
    bottom = top + len(samples) * row_height
    height = bottom + 65
    maximum = max(value for runs in samples.values() for _, value, _ in runs)
    x_max = max(1, maximum * 1.05)
    plot_width = width - left - right

    def x(value):
        return left + value * plot_width / x_max

    items = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" role="img" aria-label="Median creation-to-start wait by candidate">',
        '<rect width="100%" height="100%" fill="#fff"/>',
    ]
    for tick in range(6):
        value = x_max * tick / 5
        position = x(value)
        items += [
            f'<line x1="{position:.1f}" y1="{top}" x2="{position:.1f}" y2="{bottom}" stroke="#e5e7eb"/>',
            f'<text x="{position:.1f}" y="{bottom + 20}" text-anchor="middle">{value:.1f}</text>',
        ]
    for index, (candidate, runs) in enumerate(samples.items()):
        y = top + (index + 0.5) * row_height
        values = sorted(value for _, value, _ in runs)
        low, q1, middle, q3, high = (
            values[0],
            _percentile(values, 0.25),
            statistics.median(values),
            _percentile(values, 0.75),
            values[-1],
        )
        color = colors[candidate]
        items += [
            f'<text x="{left - 12}" y="{y + 4:.1f}" text-anchor="end">{html.escape(candidate)} (n={len(runs)})</text>',
            f'<line x1="{x(low):.1f}" y1="{y:.1f}" x2="{x(high):.1f}" y2="{y:.1f}" stroke="{color}" stroke-width="2"/>',
            f'<line x1="{x(low):.1f}" y1="{y - 12:.1f}" x2="{x(low):.1f}" y2="{y + 12:.1f}" stroke="{color}" stroke-width="2"/>',
            f'<line x1="{x(high):.1f}" y1="{y - 12:.1f}" x2="{x(high):.1f}" y2="{y + 12:.1f}" stroke="{color}" stroke-width="2"/>',
            f'<rect x="{x(q1):.1f}" y="{y - 14:.1f}" width="{max(1, x(q3) - x(q1)):.1f}" height="28" fill="{color}" fill-opacity="0.22" stroke="{color}"/>',
            f'<line x1="{x(middle):.1f}" y1="{y - 14:.1f}" x2="{x(middle):.1f}" y2="{y + 14:.1f}" stroke="{color}" stroke-width="3"/>',
            f'<text x="{x(middle) + (8 if middle <= x_max * 0.8 else -8):.1f}" '
            f'y="{y - 20:.1f}" text-anchor="{("start" if middle <= x_max * 0.8 else "end")}" '
            f'fill="{color}">{middle:,.2f} ms</text>',
        ]
        for run_id, value, count in runs:
            items.append(
                f'<circle cx="{x(value):.1f}" cy="{y:.1f}" r="3" fill="{color}">'
                f"<title>{html.escape(run_id)}: median {value:.2f} ms from {count} task waits</title></circle>"
            )
    items += [
        f'<text x="{(left + width - right) / 2:.1f}" y="{bottom + 49}" text-anchor="middle">Median creation-to-start wait per run (ms)</text>',
        "</svg>",
    ]
    path.write_text("\n".join(items) + "\n")
    return True


def _task_summary_lines(execution_dir, scenario, groups, colors):
    available = [
        trial
        for trials in groups.values()
        for trial in trials
        if trial.tasks is not None
    ]
    if not available:
        return [], []
    lines = [
        "",
        "### Pulp tasks",
        "",
        "Each value is the mean ± sample SD across runs with task data. The database snapshot includes setup and background tasks. Throughput is completed tasks divided by the first-to-last execution event window. N/A after ± means only one run had that measurement.",
        "",
        "| Candidate | Trials | Tasks | Completed | Failed | Throughput (tasks/s) | Median execution (ms) | Total execution (ms) | Median queue wait (ms) | Median time to unblock (ms) | Median ready wait (ms) |",
        "|:--|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|",
    ]
    for candidate, trials in sorted(groups.items()):
        with_tasks = [trial for trial in trials if trial.tasks is not None]
        if not with_tasks:
            continue
        trial_metrics = [task_metrics(trial.tasks) for trial in with_tasks]
        metric_names = (
            "tasks",
            "completed",
            "failed",
            "throughput",
            "median_execution_ms",
            "total_execution_ms",
            "median_wait_ms",
            "median_unblock_ms",
            "median_ready_ms",
        )
        lines.append(
            f"| {markdown_value(candidate)} | {len(with_tasks)} | "
            + " | ".join(
                _mean_sd([item[name] for item in trial_metrics])
                for name in metric_names
            )
            + " |"
        )
    lines += [
        "",
        "#### Most frequent task types",
        "",
        "| Candidate | Task type | Tasks | Completed | Failed | Total execution (ms) |",
        "|:--|:--|--:|--:|--:|--:|",
    ]
    grouped = defaultdict(list)
    for candidate, trials in groups.items():
        for trial in trials:
            for row in trial.tasks or []:
                grouped[(candidate, row["task_type"] or "Unknown")].append(row)
    for candidate in sorted(groups):
        types = [
            (task_type, rows)
            for (name, task_type), rows in grouped.items()
            if name == candidate
        ]
        for task_type, rows in sorted(types, key=lambda item: (-len(item[1]), item[0]))[
            :10
        ]:
            durations = [
                row["execution_time_us"] / 1000
                for row in rows
                if row["execution_time_us"] is not None
            ]
            lines.append(
                f"| {markdown_value(candidate)} | {markdown_value(task_type)} | {len(rows)} | "
                f"{sum(row['status'] == 'completed' for row in rows)} | "
                f"{sum(row['status'] == 'failed' for row in rows)} | "
                f"{_format(sum(durations) if durations else None, 2)} |"
            )
    lines += [
        "",
        "#### Total execution time by task type",
        "",
        "Totals sum the reported execution time of all instances across the listed runs.",
        "",
        "| Candidate | Task type | Total execution (ms) |",
        "|:--|:--|--:|",
    ]
    for candidate in sorted(groups):
        types = []
        for (name, task_type), rows in grouped.items():
            if name == candidate:
                durations = [
                    row["execution_time_us"] / 1000
                    for row in rows
                    if row["execution_time_us"] is not None
                ]
                if durations:
                    types.append((task_type, sum(durations)))
        for task_type, total in sorted(types, key=lambda item: (-item[1], item[0]))[
            :10
        ]:
            lines.append(
                f"| {markdown_value(candidate)} | {markdown_value(task_type)} | {_format(total, 2)} |"
            )
    lines.append("")
    safe_scenario = re.sub(r"[^A-Za-z0-9_-]+", "_", scenario)
    name = f"trials_{safe_scenario}_wait_boxplot.svg"
    distribution = []
    if _wait_boxplot(execution_dir / name, groups, colors):
        distribution = [
            "### Wait-time distribution",
            "",
            "#### Creation-to-start wait by candidate",
            "",
            "Each point is one run's median creation-to-start wait. Boxes span the 25th–75th percentiles of run medians; center lines show medians and whiskers show the minimum and maximum. Runs without measured waits are omitted. Hover over points for run IDs and sample counts.",
            "",
            f"![Creation-to-start wait by candidate]({name})",
            "",
        ]
    for field, title, suffix in (
        ("time_to_unblock_us", "Time to unblock", "unblock"),
        ("ready_wait_us", "Ready queue wait", "ready_wait"),
    ):
        traces = [
            (
                candidate,
                colors[candidate],
                [
                    row[field] / 1000
                    for trial in trials
                    for row in trial.tasks or []
                    if row[field] is not None
                ],
                sum(len(trial.tasks or []) for trial in trials),
            )
            for candidate, trials in sorted(groups.items())
        ]
        filename = f"trials_{safe_scenario}_{suffix}_histogram.svg"
        if wait_distribution_chart(execution_dir / filename, traces, title):
            distribution += [
                f"#### {title} distribution by candidate",
                "",
                "Bars pool task observations across runs within each candidate. Candidate summary statistics above give each run equal weight.",
                "",
                f"![{title} distribution by candidate]({filename})",
                "",
            ]
    return lines, distribution


def _median_curve(runs):
    """Pointwise median, interpolating only inside each run's observed window."""
    series = []
    for _, points in runs:
        ordered = sorted(dict(points).items())
        if ordered:
            series.append(([x for x, _ in ordered], [y for _, y in ordered]))
    xs = sorted({x for times, _ in series for x in times})
    curve = []
    for x in xs:
        values = []
        for times, heights in series:
            if x < times[0] or x > times[-1]:
                continue
            index = bisect_right(times, x) - 1
            if times[index] == x or index == len(times) - 1:
                values.append(heights[index])
            else:
                ratio = (x - times[index]) / (times[index + 1] - times[index])
                values.append(
                    heights[index] + ratio * (heights[index + 1] - heights[index])
                )
        if values:
            curve.append((x, statistics.median(values)))
    return curve


def _smoothed_mean_rate(runs, bandwidth_s=3):
    """Smooth the run mean rate while preserving its request-count integral."""
    counts = Counter()
    for _, points in runs:
        for second, count in points:
            counts[int(second)] += count / len(runs)
    if not counts:
        return []
    radius = math.ceil(3 * bandwidth_s)
    last_second = max(counts) + radius
    weights = {
        offset: math.exp(-0.5 * (offset / bandwidth_s) ** 2)
        for offset in range(-radius, radius + 1)
    }
    smoothed = [0.0] * (last_second + 1)
    for second, count in counts.items():
        if not count:
            continue
        positions = range(
            max(0, second - radius), min(last_second, second + radius) + 1
        )
        normalizer = sum(weights[position - second] for position in positions)
        for position in positions:
            smoothed[position] += count * weights[position - second] / normalizer
    area = sum((a + b) / 2 for a, b in itertools.pairwise(smoothed))
    scale = sum(counts.values()) / area if area else 1
    return [(second, value * scale) for second, value in enumerate(smoothed)]


def _chart(
    path: Path,
    label: str,
    groups,
    *,
    summary_fn=_median_curve,
    summary_label="median",
    request_overlays=None,
    minimum_y=1,
):
    if not groups:
        return
    summaries = {candidate: summary_fn(runs) for candidate, _, runs in groups}
    request_overlays = request_overlays or {}
    width, left, right = (1080, 84, 174) if request_overlays else (960, 84, 24)
    top, plot_height = 30, 300
    bottom = top + plot_height
    legend_top = bottom + 66
    height = legend_top + len(groups) * 44 + (55 if request_overlays else 18)
    values = [point for _, _, runs in groups for _, series in runs for point in series]
    values += [point for points in summaries.values() for point in points]
    x_values = values + [
        point for points in request_overlays.values() for point in points
    ]
    x_min = min(0, *(x for x, _ in x_values))
    x_max = max(1, *(x for x, _ in x_values))
    y_max = max(minimum_y, *(y for _, y in values)) * 1.05
    plot_width = width - left - right

    def xy(x, y):
        return (
            left + (x - x_min) * plot_width / (x_max - x_min),
            bottom - y * plot_height / y_max,
        )

    items = [
        (
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
            f'role="img" aria-label="{html.escape(label, quote=True)}">'
        ),
        '<rect width="100%" height="100%" fill="#fff"/>',
    ]
    for index in range(5):
        value = y_max * index / 4
        y = xy(0, value)[1]
        items += [
            f'<line x1="{left}" y1="{y:.1f}" x2="{width - right}" y2="{y:.1f}" stroke="#e5e7eb"/>',
            f'<text x="{left - 8}" y="{y + 4:.1f}" text-anchor="end">{value:g}</text>',
        ]
        time = x_min + (x_max - x_min) * index / 4
        x = xy(time, 0)[0]
        items.append(
            f'<text x="{x:.1f}" y="{bottom + 20}" text-anchor="middle">{time:.0f}</text>'
        )
    if x_min < 0 < x_max:
        zero = xy(0, 0)[0]
        items.append(
            f'<line x1="{zero:.1f}" y1="{top}" x2="{zero:.1f}" y2="{bottom}" '
            'stroke="#94a3b8" stroke-dasharray="3 3"/>'
        )
    items += [
        f'<line x1="{left}" y1="{top}" x2="{left}" y2="{bottom}" stroke="#94a3b8"/>',
        f'<line x1="{left}" y1="{bottom}" x2="{width - right}" y2="{bottom}" stroke="#94a3b8"/>',
    ]
    for index, (candidate, color, runs) in enumerate(groups):
        legend_y = legend_top + index * 44
        for run_id, points in runs:
            coordinates = " ".join(
                f"{x:.1f},{y:.1f}" for x, y in (xy(*point) for point in points)
            )
            items.append(
                f'<polyline points="{coordinates}" fill="none" stroke="{color}" '
                'stroke-width="1.5" stroke-opacity="0.22" data-series="run" '
                f'data-candidate="{html.escape(candidate, quote=True)}" data-run="{html.escape(run_id, quote=True)}">'
                f"<title>{html.escape(candidate)} / {html.escape(run_id)}</title></polyline>"
            )
        summary_points = summaries[candidate]
        coordinates = " ".join(
            f"{x:.1f},{y:.1f}" for x, y in (xy(*point) for point in summary_points)
        )
        items.append(
            f'<polyline points="{coordinates}" fill="none" stroke="{color}" '
            f'stroke-width="3.5" data-series="{summary_label.replace(" ", "-")}" '
            f'data-candidate="{html.escape(candidate, quote=True)}"/>'
        )
        for offset, name, opacity, thickness in (
            (0, f"{candidate} runs (n={len(runs)})", "0.22", "1.5"),
            (22, f"{candidate} {summary_label}", "1", "3.5"),
        ):
            y = legend_y + offset
            items += [
                f'<line x1="{left}" y1="{y}" x2="{left + 25}" y2="{y}" stroke="{color}" stroke-opacity="{opacity}" stroke-width="{thickness}"/>',
                f'<text x="{left + 34}" y="{y + 4}">{html.escape(name)}</text>',
            ]
    for index, (key, title, color, axis_title, dashed) in enumerate(
        (
            (
                "completed",
                "Median completed requests",
                "#64748b",
                "Completed requests",
                True,
            ),
            ("starts", "Median new requests/s", "#7c3aed", "New requests/s", False),
        )
    ):
        points = request_overlays.get(key)
        if not points:
            continue
        scale = max(1, *(value for _, value in points))
        position = width - right + index * 96
        axis_x = position + 55
        dash = ' stroke-dasharray="5 5"' if dashed else ""
        items.append(
            f'<line x1="{position}" y1="{top}" x2="{position}" y2="{bottom}" stroke="#94a3b8"/>'
        )
        for fraction in (0, 0.5, 1):
            y = bottom - fraction * plot_height
            items.append(
                f'<text x="{position + 8}" y="{y + 4:.1f}" font-family="sans-serif" '
                f'font-size="12" fill="#64748b">{scale * fraction:g}</text>'
            )
        items.append(
            f'<text x="{axis_x}" y="{top + plot_height / 2}" '
            f'transform="rotate(90 {axis_x} {top + plot_height / 2})" text-anchor="middle" '
            f'font-family="sans-serif" font-size="12" fill="#475569">{axis_title}</text>'
        )
        coordinates = " ".join(
            f"{xy(x, 0)[0]:.1f},{bottom - value * plot_height / scale:.1f}"
            for x, value in points
        )
        items.append(
            f'<polyline points="{coordinates}" fill="none" stroke="{color}" '
            f'stroke-width="2"{dash} data-series="request-{key}"/>'
        )
        legend_y = legend_top + len(groups) * 44 + index * 22
        items += [
            f'<line x1="{left}" y1="{legend_y}" x2="{left + 25}" y2="{legend_y}" '
            f'stroke="{color}" stroke-width="2"{dash}/>',
            f'<text x="{left + 34}" y="{legend_y + 4}" font-family="sans-serif" '
            f'font-size="12" fill="#475569">{title} (right)</text>',
        ]
    items += [
        (
            f'<text x="{width / 2}" y="{bottom + 45}" text-anchor="middle">'
            "Time since load start (s)</text>"
        ),
        (
            f'<text x="20" y="{top + plot_height / 2}" '
            f'transform="rotate(-90 20 {top + plot_height / 2})" text-anchor="middle">'
            f"{html.escape(label)}</text>"
        ),
        "</svg>",
    ]
    path.write_text("\n".join(items) + "\n")


def _task_gantt(path: Path, trials: list[Trial]) -> int | None:
    """Draw observed task durations from the logged start and finish times."""
    bars = []
    for trial in trials:
        tasks = trial.tasks or []
        starts = [row["started_at_utc"] for row in tasks if row["started_at_utc"]]
        if not starts:
            continue
        saved_start = (trial.metadata or {}).get("result", {}).get("load_start_utc")
        origin = datetime.fromisoformat(saved_start) if saved_start else min(starts)
        for row in tasks:
            start = row["started_at_utc"]
            finish = row["finished_at_utc"]
            if (
                start is None
                or finish is None
                or finish < start
                or row["status"] not in {"completed", "failed"}
            ):
                continue
            bars.append(
                (
                    trial.run_id,
                    row["task_type"] or "Unknown",
                    row["task_id"],
                    row["status"],
                    (start - origin).total_seconds(),
                    (finish - origin).total_seconds(),
                )
            )
    if not bars:
        return None
    bars.sort(key=lambda bar: (bar[0], bar[4], bar[1], bar[2]))
    left, right = max(320, 28 + 7 * max(len(bar[1]) for bar in bars)), 30
    width = left + 750 + right
    top, row_height, run_height = 58, 18, 24
    bottom = top + len(bars) * row_height + len({bar[0] for bar in bars}) * run_height
    height = bottom + 65
    first = min(0, *(bar[4] for bar in bars))
    last = max(1, *(bar[5] for bar in bars))
    plot_width = width - left - right

    def x(second):
        return left + (second - first) * plot_width / (last - first)

    items = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" role="img" aria-label="Task duration Gantt chart">',
        '<rect width="100%" height="100%" fill="#fff"/>',
    ]
    for index in range(5):
        second = first + (last - first) * index / 4
        position = x(second)
        items += [
            f'<line x1="{position:.1f}" y1="{top}" x2="{position:.1f}" y2="{bottom}" stroke="#e5e7eb"/>',
            f'<text x="{position:.1f}" y="{bottom + 20}" text-anchor="middle">{second:.1f}</text>',
        ]
    y = top
    previous_run = None
    for run, task_type, task_id, status, start, finish in bars:
        if run != previous_run:
            items += [
                f'<rect x="64" y="{y}" width="{width - right - 64}" height="{run_height}" fill="#f1f5f9"/>',
                f'<text x="72" y="{y + 16}" font-size="12" font-weight="bold">{html.escape(run)}</text>',
            ]
            y += run_height
            previous_run = run
        color = "#2563eb" if status == "completed" else "#dc2626"
        duration = finish - start
        x_start = x(start)
        bar_width = max(2, x(finish) - x_start)
        safe_run = html.escape(run, quote=True)
        safe_task_id = html.escape(task_id, quote=True)
        items += [
            f'<text x="{left - 8}" y="{y + 13}" text-anchor="end" font-size="11">{html.escape(task_type)}</text>',
            f'<rect x="{x_start:.1f}" y="{y + 3}" width="{bar_width:.1f}" height="12" fill="{color}" data-run="{safe_run}" data-task-id="{safe_task_id}" data-start-s="{start:.3f}" data-duration-s="{duration:.3f}">',
            f"<title>{html.escape(task_type)}: {duration:.3f} s ({status})</title>",
            "</rect>",
        ]
        y += row_height
    items += [
        f'<line x1="{left}" y1="{top}" x2="{left}" y2="{bottom}" stroke="#94a3b8"/>',
        f'<line x1="{left}" y1="{bottom}" x2="{width - right}" y2="{bottom}" stroke="#94a3b8"/>',
        f'<text x="{width / 2}" y="{bottom + 45}" text-anchor="middle">Time from run origin (s)</text>',
        f'<text x="18" y="{(top + bottom) / 2}" transform="rotate(-90 18 {(top + bottom) / 2})" text-anchor="middle">Task instances</text>',
        f'<rect x="{left}" y="22" width="12" height="12" fill="#2563eb"/>',
        f'<text x="{left + 20}" y="33">Completed</text>',
        f'<rect x="{left + 130}" y="22" width="12" height="12" fill="#dc2626"/>',
        f'<text x="{left + 150}" y="33">Failed</text>',
        "</svg>",
    ]
    path.write_text("\n".join(items) + "\n")
    return width


def _timing_lines(trials, timings):
    matched = [
        (trial, timings[(trial.scenario, trial.candidate, trial.run_id)])
        for trial in trials
        if (trial.scenario, trial.candidate, trial.run_id) in timings
    ]
    if not matched:
        return []
    lines = [
        "",
        "### Trial time by phase",
        "",
        "Values are mean ± sample SD across trials, from `execution.log` timestamps (one-second precision). Preparation includes fixture, source, service, and pre-test setup. Load test is the active Locust interval. Total spans the trial start and finish markers. Missing phase markers are omitted from that phase's mean; SD is N/A with one measurement.",
        "",
        "| Candidate | Trials | Preparation (s) | Locust startup (s) | Load test (s) | Locust shutdown (s) | Cleanup (s) | Total per trial (s) | Sum total (s) |",
        "|:--|--:|--:|--:|--:|--:|--:|--:|--:|",
    ]
    for candidate in sorted({trial.candidate for trial, _ in matched}):
        candidate_timings = [
            timing for trial, timing in matched if trial.candidate == candidate
        ]
        phases = " | ".join(
            _mean_sd([timing.durations_s[phase] for timing in candidate_timings], 1)
            for phase, _, _ in PHASES
        )
        lines.append(
            f"| {markdown_value(candidate)} | {len(candidate_timings)} | {phases} | "
            f"{_mean_sd([timing.total_s for timing in candidate_timings], 1)} | "
            f"{_format(sum(timing.total_s for timing in candidate_timings), 1)} |"
        )
    lines += [
        "",
        f"**Total time across {len(matched)} timed trials:** {_format(sum(timing.total_s for _, timing in matched) / 60, 2)} minutes.",
    ]
    if len(matched) < len(trials):
        lines += [
            "",
            f"Timing unavailable for {len(trials) - len(matched)} trial(s) without complete start and finish markers.",
        ]
    return lines


def _scenario_sections(execution_dir, scenario, trials, colors, timings):
    groups = defaultdict(list)
    for trial in trials:
        groups[trial.candidate].append(trial)
    lines = [
        f"## {markdown_value(scenario)}",
        "",
        "### Candidate summaries",
        "",
        "Each value is the mean ± sample SD across runs (n − 1 denominator). Unavailable measurements are omitted; SD is N/A when only one run has a value. Requests within a run are not independent trials.",
        "",
        "| Candidate | Trials | Requests | Failures | Failure rate (%) | Requests/s | Mean latency (ms) | Run p95 (ms) | Mean DB CPU (%) | Mean DB PSS (MiB) |",
        "|:--|--:|--:|--:|--:|--:|--:|--:|--:|--:|",
    ]
    for candidate, candidate_trials in sorted(groups.items()):
        lines.append(
            f"| {markdown_value(candidate)} | {len(candidate_trials)} | "
            + " | ".join(
                _mean_sd([getattr(trial, attribute) for trial in candidate_trials])
                for attribute in (
                    "requests",
                    "failures",
                    "failure_pct",
                    "rate",
                    "mean_ms",
                    "p95_ms",
                    "cpu_pct",
                    "memory_mib",
                )
            )
            + " |"
        )
    names = sorted(groups)
    if len(names) > 1:
        baseline = names[0]
        lines += [
            "",
            f"### Candidate differences (relative to {markdown_value(baseline)})",
            "",
            "Differences are candidate mean minus baseline mean. These are descriptive comparisons of runs, not confidence intervals.",
            "",
            "| Candidate | Δ Requests/s | Δ Mean latency (ms) | Δ Run p95 (ms) | Δ Failure rate (pp) | Δ Mean DB CPU (pp) | Δ Mean DB PSS (MiB) |",
            "|:--|--:|--:|--:|--:|--:|--:|",
        ]
        for candidate in names[1:]:
            differences = []
            for attribute in (
                "rate",
                "mean_ms",
                "p95_ms",
                "failure_pct",
                "cpu_pct",
                "memory_mib",
            ):
                current = _contrast(groups[candidate], attribute)
                reference = _contrast(groups[baseline], attribute)
                differences.append(
                    _format(current - reference, 2)
                    if current is not None and reference is not None
                    else "N/A"
                )
            lines.append(
                f"| {markdown_value(candidate)} | " + " | ".join(differences) + " |"
            )
    signatures = {
        (
            json.dumps(trial.metadata.get("load"), sort_keys=True),
            trial.metadata.get("fixture", {}).get("manifest_sha256"),
        )
        for trial in trials
        if trial.metadata
    }
    if len(signatures) > 1:
        lines += [
            "",
            "Run configurations or fixture contents differ. Interpret candidate differences with care.",
        ]
    if any(trial.metadata is None for trial in trials):
        lines += [
            "",
            "Some runs have no run metadata, so their completion and configuration could not be verified.",
        ]
    if any(trial.content_io_summary for trial in trials):
        lines += [
            "",
            "### Content-worker I/O",
            "",
            "Each value is the mean ± sample SD of run-level measurements. Storage bytes come from Linux process counters and do not include network traffic. Byte totals approximate the observed counter changes after each worker's first sample.",
            "",
            "| Candidate | Trials | Read (MiB) | Written (MiB) | Peak read (MiB/s) | Peak write (MiB/s) | Read syscalls | Write syscalls |",
            "|:--|--:|--:|--:|--:|--:|--:|--:|",
        ]
        for candidate, candidate_trials in sorted(groups.items()):
            summaries = [
                trial.content_io_summary
                for trial in candidate_trials
                if trial.content_io_summary
            ]
            lines.append(
                f"| {markdown_value(candidate)} | {len(summaries)} | "
                + " | ".join(
                    _mean_sd([summary[key] for summary in summaries])
                    for key in (
                        "read_mib",
                        "write_mib",
                        "peak_read_mib_s",
                        "peak_write_mib_s",
                        "read_syscalls",
                        "write_syscalls",
                    )
                )
                + " |"
            )
    if any(trial.temp_summary for trial in trials):
        lines += [
            "",
            "### Pulp temporary files",
            "",
            "Each value is the mean ± sample SD of each run's sampled peak. The working directory is shared by content downloads, API uploads, and tasks; short-lived files can fall between samples.",
            "",
            "| Candidate | Trials | Peak files | Peak apparent size (MiB) | Peak root-level size (MiB) | Peak allocated size (MiB) |",
            "|:--|--:|--:|--:|--:|--:|",
        ]
        for candidate, candidate_trials in sorted(groups.items()):
            summaries = [
                trial.temp_summary for trial in candidate_trials if trial.temp_summary
            ]
            lines.append(
                f"| {markdown_value(candidate)} | {len(summaries)} | "
                + " | ".join(
                    _mean_sd([summary[key] for summary in summaries])
                    for key in (
                        "peak_files",
                        "peak_mib",
                        "peak_root_mib",
                        "peak_allocated_mib",
                    )
                )
                + " |"
            )
    lines.extend(_timing_lines(trials, timings))
    task_summary, distribution = _task_summary_lines(
        execution_dir, scenario, groups, colors
    )
    lines.extend(task_summary)
    graphs = [
        f"## {markdown_value(scenario)}",
        "",
        *distribution,
        "### Time overlays",
        "",
        (
            "Time zero is the recorded load start; older results use the first request start. "
            "Faint lines show individual runs in each candidate's color. For completed requests per second, thick lines show a Gaussian-smoothed mean (3 s bandwidth); their area equals the mean completed-request count. Other thick lines show the pointwise median across runs observed at each time, with no extrapolation beyond a run's observed window. On content I/O charts, gray and purple lines show the median completed requests and new requests per second across the displayed runs, using the labeled right axes."
        ),
        "",
    ]
    for metric, title, axis_label in _METRICS:
        chart_groups = []
        for candidate in names:
            runs = [
                (trial.run_id, trial.series[metric])
                for trial in groups[candidate]
                if trial.series.get(metric)
            ]
            if runs:
                chart_groups.append((candidate, colors[candidate], runs))
        if chart_groups:
            safe_scenario = re.sub(r"[^A-Za-z0-9_-]+", "_", scenario)
            name = f"trials_{safe_scenario}_{metric}.svg"
            if metric == "throughput":
                _chart(
                    execution_dir / name,
                    axis_label,
                    chart_groups,
                    summary_fn=_smoothed_mean_rate,
                    summary_label="smoothed mean",
                )
            else:
                request_overlays = None
                if metric in ("content_read", "content_write"):
                    displayed = [trial for trial in trials if trial.series.get(metric)]
                    request_overlays = {
                        key: curve
                        for key in ("completed", "starts")
                        if (
                            curve := _median_curve(
                                [
                                    (trial.run_id, trial.series[key])
                                    for trial in displayed
                                    if trial.series.get(key)
                                ]
                            )
                        )
                    }
                _chart(
                    execution_dir / name,
                    axis_label,
                    chart_groups,
                    request_overlays=request_overlays,
                    minimum_y=0.01 if metric.startswith("content_") else 1,
                )
            graphs += [f"#### {title}", "", f"![{title}]({name})", ""]
    if not any(trial.series for trial in trials):
        graphs += ["No request timelines available.", ""]
    gantt = []
    safe_scenario = re.sub(r"[^A-Za-z0-9_-]+", "_", scenario)
    for candidate in names:
        safe_candidate = re.sub(r"[^A-Za-z0-9_-]+", "_", candidate)
        name = f"trials_{safe_scenario}_{safe_candidate}_task_gantt.svg"
        gantt_width = _task_gantt(execution_dir / name, groups[candidate])
        if gantt_width:
            gantt += [
                f"#### {markdown_value(candidate)}",
                "",
                '::: {style="max-height: 720px; overflow: auto;"}',
                f"![Task duration Gantt chart]({name}){{width={gantt_width}}}",
                ":::",
                "",
            ]
    if gantt:
        graphs += [
            "### Task durations by candidate",
            "",
            "Each bar spans a task's database start and finish times. Time zero is the recorded load start, or the first task start when unavailable. Tasks missing either timestamp are omitted. Colors show completion or failure.",
            "",
            *gantt,
        ]
    return lines, graphs


def render_trial_report(
    execution_dir: Path, quarto_command: str = "quarto"
) -> Path | None:
    """Render run-level summaries and candidate comparisons for one execution."""
    execution_dir = Path(execution_dir)
    report = execution_dir / "trials.html"
    if report.is_file():
        source = execution_dir / "trials.qmd"
        task_data_is_newer = any(
            path.stat().st_mtime_ns > report.stat().st_mtime_ns
            for path in execution_dir.glob("*/*/*/collectors/tasking.parquet")
        )
        if not task_data_is_newer and (
            not source.is_file() or _TRIAL_LAYOUT_MARKER in source.read_text()
        ):
            return report.resolve()
    trials = [
        trial
        for run_dir in completed_runs(execution_dir)
        if (trial := _read_trial(run_dir)) is not None
    ]
    if not trials:
        return None
    quarto = shutil.which(quarto_command)
    if quarto is None:
        raise FileNotFoundError(
            f"Quarto executable not found: {quarto_command}. "
            "Install Quarto or run `make setup` to install the report dependencies."
        )
    candidates = sorted({trial.candidate for trial in trials})
    colors = {
        candidate: _COLORS[index % len(_COLORS)]
        for index, candidate in enumerate(candidates)
    }
    groups = defaultdict(list)
    for trial in trials:
        groups[trial.scenario].append(trial)
    timings = parse_execution_timings(execution_dir / "execution.log")
    lines = [
        "---",
        f"title: {json.dumps(f'Trial comparison: {execution_dir.name}')}",
        "format:",
        "  html:",
        "    toc: true",
        "    embed-resources: true",
        "---",
        "",
        _TRIAL_LAYOUT_MARKER,
        "",
        "# Trial and candidate comparison",
        "",
        (
            f"Execution: `{markdown_value(execution_dir.name)}`. "
            "[Detailed run report](report.html)."
        ),
        "",
        (
            "Each run is one trial. Candidate summaries give every run equal weight. "
            "Runs from other executions are not included. Run order and shared host conditions may affect candidate differences."
        ),
        "",
        "# Summaries",
        "",
    ]
    graphs = ["# Graphs", ""]
    for scenario, scenario_trials in sorted(groups.items()):
        scenario_trials.sort(key=lambda trial: trial.run_id)
        summary, scenario_graphs = _scenario_sections(
            execution_dir, scenario, scenario_trials, colors, timings
        )
        lines.extend(summary)
        graphs.extend(scenario_graphs)
    lines.extend(graphs)
    source = execution_dir / "trials.qmd"
    source.write_text("\n".join(lines))
    subprocess.run(
        [
            str(Path(quarto).resolve()),
            "render",
            source.name,
            "--to",
            "html",
            "--output",
            report.name,
            "--quiet",
        ],
        cwd=execution_dir,
        check=True,
    )
    return report.resolve()
