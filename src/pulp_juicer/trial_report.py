"""Compare runs as trials within each scenario of one execution."""

import html
import json
import math
import re
import shutil
import statistics
import subprocess
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pyarrow.parquet as pq

from .report import completed_runs, markdown_value, read_csv
from .tasking import task_metrics, wait_distribution_chart

_COLORS = ("#2563eb", "#e56b35", "#16a34a", "#a855f7", "#dc2626", "#0891b2")
_DASHES = ("", "8 4", "3 3", "11 4 3 4")
_TRIAL_LAYOUT_MARKER = "<!-- pulp-juicer-trial-layout: 13 -->"
_METRICS = (
    ("completed", "Cumulative completed requests", "Completed requests (count)"),
    ("throughput", "Completed requests per second", "Completed requests/s"),
    ("starts", "New requests per second", "New requests/s"),
    ("cpu", "PostgreSQL CPU", "PostgreSQL CPU (% of one core)"),
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
    if not request_path.is_file():
        return {}
    rows = read_csv(request_path)
    if not rows:
        return {}
    starts = [datetime.fromisoformat(row["started_at_utc"]).timestamp() for row in rows]
    completions = [
        datetime.fromisoformat(row["completed_at_utc"]).timestamp() for row in rows
    ]
    saved_start = (metadata or {}).get("result", {}).get("load_start_utc")
    origin = (
        datetime.fromisoformat(saved_start).timestamp() if saved_start else min(starts)
    )
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
    series = {
        "completed": completed,
        "throughput": throughput,
        "starts": new_requests,
    }
    cpu_path = run_dir / "db_cpu.csv"
    if cpu_path.is_file():
        series["cpu"] = [
            (
                datetime.fromisoformat(row["timestamp_utc"]).timestamp() - origin,
                float(row["postgres_cpu_pct"]),
            )
            for row in read_csv(cpu_path)
            if row.get("timestamp_utc") and row.get("postgres_cpu_pct")
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
    cpu_path = run_dir / "db_cpu_summary.csv"
    cpu_rows = read_csv(cpu_path) if cpu_path.is_file() else []
    requests = int(stats["Request Count"])
    task_path = run_dir / "tasking.parquet"
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
        cpu_pct=_float(cpu_rows[0], "postgres_cpu_mean_pct") if cpu_rows else None,
        metadata=metadata,
        series=series,
        tasks=tasks,
    )


def _format(value, digits=1):
    return f"{value:,.{digits}f}" if value is not None else "N/A"


def _percent(value, digits=2):
    return f"{_format(value, digits)}%" if value is not None else "N/A"


def _spread(trials, attribute, digits=1):
    values = sorted(
        value for trial in trials if (value := getattr(trial, attribute)) is not None
    )
    if not values:
        return "N/A"
    middle = _format(statistics.median(values), digits)
    if len(values) == 1:
        return middle
    return f"{middle} ({_format(values[0], digits)}–{_format(values[-1], digits)})"


def _contrast(trials, attribute):
    values = [
        value for trial in trials if (value := getattr(trial, attribute)) is not None
    ]
    return statistics.median(values) if values else None


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
        "Task records include setup and background activity from Compose logs. Throughput is completed tasks divided by the first-to-last task event window. Waiting time is available only for tasks with matching dispatch logs, currently scheduled tasks.",
        "",
        "| Candidate | Run | Tasks | Completed | Failed | Throughput (tasks/s) | Task types | Workers | Median execution (ms) | Max execution (ms) | Wait samples | Median wait (ms) | Max wait (ms) |",
        "|:--|:--|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|",
    ]
    for candidate, trials in sorted(groups.items()):
        for trial in trials:
            if trial.tasks is None:
                continue
            metrics = task_metrics(trial.tasks)
            lines.append(
                f"| {markdown_value(candidate)} | {markdown_value(trial.run_id)} | "
                f"{metrics['tasks']} | {metrics['completed']} | {metrics['failed']} | "
                f"{_format(metrics['throughput'], 2)} | {metrics['task_types']} | "
                f"{metrics['workers']} | {_format(metrics['median_execution_ms'], 2)} | "
                f"{_format(metrics['max_execution_ms'], 2)} | {metrics['wait_count']} | "
                f"{_format(metrics['median_wait_ms'], 2)} | {_format(metrics['max_wait_ms'], 2)} |"
            )
    lines += [
        "",
        "#### Task counts by candidate",
        "",
        "Each value is the median across runs, followed by the run range in parentheses.",
        "",
        "| Candidate | Trials with task data | Tasks | Completed | Failed | Throughput (tasks/s) | Median execution (ms) | Median wait (ms) |",
        "|:--|--:|--:|--:|--:|--:|--:|--:|",
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
            "median_wait_ms",
        )
        metrics = [
            [item[name] for item in trial_metrics if item[name] is not None]
            for name in metric_names
        ]

        def spread(values, digits=0):
            if not values:
                return "N/A"
            middle = _format(statistics.median(values), digits)
            return (
                middle
                if len(values) == 1
                else f"{middle} ({_format(min(values), digits)}–{_format(max(values), digits)})"
            )

        lines.append(
            f"| {markdown_value(candidate)} | {len(with_tasks)} | "
            + " | ".join(
                spread(values, 2 if index >= 3 else 0)
                for index, values in enumerate(metrics)
            )
            + " |"
        )
    lines += [
        "",
        "#### Most frequent task types",
        "",
        "| Candidate | Task type | Tasks | Completed | Failed | Max execution (ms) |",
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
                f"{_format(max(durations) if durations else None, 2)} |"
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
    traces = [
        (
            f"{candidate} / {trial.run_id}",
            colors[candidate],
            [
                row["waiting_time_us"] / 1000
                for row in trial.tasks or []
                if row.get("waiting_time_us") is not None
            ],
            len(trial.tasks or []),
        )
        for candidate, trials in sorted(groups.items())
        for trial in trials
    ]
    safe_scenario = re.sub(r"[^A-Za-z0-9_-]+", "_", scenario)
    name = f"trials_{safe_scenario}_wait_distribution.svg"
    distribution = []
    if wait_distribution_chart(execution_dir / name, traces):
        distribution = [
            "### Wait-time distribution",
            "",
            "#### Dispatch-to-start wait distribution",
            "",
            "Each color shows one run; bars within each labeled millisecond range are side by side. Counts include only tasks with both dispatch and start log entries. The legend shows coverage against all logged tasks.",
            "",
            f"![Dispatch-to-start wait distribution]({name})",
            "",
        ]
    return lines, distribution


def _chart(path: Path, label: str, traces: list[tuple[str, str, str, list]]):
    if not traces:
        return
    width, left, right = 960, 84, 24
    top, plot_height = 30, 300
    bottom = top + plot_height
    legend_top = bottom + 66
    height = legend_top + len(traces) * 22 + 18
    values = [point for _, _, _, series in traces for point in series]
    x_min = min(0, *(x for x, _ in values))
    x_max = max(1, *(x for x, _ in values))
    y_max = max(1, *(y for _, y in values)) * 1.05
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
            f'<text x="{left - 8}" y="{y + 4:.1f}" text-anchor="end">{value:.0f}</text>',
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
    for index, (name, color, dash, points) in enumerate(traces):
        coordinates = " ".join(
            f"{x:.1f},{y:.1f}" for x, y in (xy(*point) for point in points)
        )
        dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
        items += [
            (
                f'<polyline points="{coordinates}" fill="none" stroke="{color}" '
                f'stroke-width="2.5"{dash_attr}/>'
            ),
            (
                f'<line x1="{left}" y1="{legend_top + index * 22}" '
                f'x2="{left + 25}" y2="{legend_top + index * 22}" '
                f'stroke="{color}" stroke-width="2.5"{dash_attr}/>'
            ),
            (
                f'<text x="{left + 34}" y="{legend_top + index * 22 + 4}">'
                f"{html.escape(name)}</text>"
            ),
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


def _scenario_sections(execution_dir, scenario, trials, colors):
    lines = [
        f"## {markdown_value(scenario)}",
        "",
        "### Trials",
        "",
        "| Candidate | Run | Completed | Failures | Failure rate | Requests/s | Mean latency (ms) | Run p95 (ms) | Mean DB CPU (%) |",
        "|:--|:--|--:|--:|--:|--:|--:|--:|--:|",
    ]
    for trial in trials:
        lines.append(
            f"| {markdown_value(trial.candidate)} | {markdown_value(trial.run_id)} | "
            f"{trial.requests} | {trial.failures} | {_percent(trial.failure_pct)} | "
            f"{_format(trial.rate, 2)} | {_format(trial.mean_ms)} | "
            f"{_format(trial.p95_ms)} | {_format(trial.cpu_pct, 2)} |"
        )
    groups = defaultdict(list)
    for trial in trials:
        groups[trial.candidate].append(trial)
    lines += [
        "",
        "### Candidate summaries",
        "",
        (
            "Each value is the median across runs, followed by the run range in parentheses. "
            "Unavailable metrics are omitted from their summaries. Requests within a run are not independent trials."
        ),
        "",
        "| Candidate | Trials | Requests/s | Mean latency (ms) | Median run p95 (ms) | Failure rate (%) | Mean DB CPU (%) |",
        "|:--|--:|--:|--:|--:|--:|--:|",
    ]
    for candidate, candidate_trials in sorted(groups.items()):
        lines.append(
            f"| {markdown_value(candidate)} | {len(candidate_trials)} | "
            f"{_spread(candidate_trials, 'rate', 2)} | "
            f"{_spread(candidate_trials, 'mean_ms')} | "
            f"{_spread(candidate_trials, 'p95_ms')} | "
            f"{_spread(candidate_trials, 'failure_pct', 2)} | "
            f"{_spread(candidate_trials, 'cpu_pct', 2)} |"
        )
    names = sorted(groups)
    if len(names) > 1:
        baseline = names[0]
        lines += [
            "",
            f"### Candidate differences (relative to {markdown_value(baseline)})",
            "",
            "Differences are candidate median minus baseline median. These are descriptive comparisons of runs, not confidence intervals.",
            "",
            "| Candidate | Δ Requests/s | Δ Mean latency (ms) | Δ Median run p95 (ms) | Δ Failure rate (pp) | Δ Mean DB CPU (pp) |",
            "|:--|--:|--:|--:|--:|--:|",
        ]
        for candidate in names[1:]:
            differences = []
            for attribute in ("rate", "mean_ms", "p95_ms", "failure_pct", "cpu_pct"):
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
            "Lines show individual runs. Colors identify candidates; line patterns distinguish runs."
        ),
        "",
    ]
    for metric, title, axis_label in _METRICS:
        traces = []
        for candidate in names:
            for index, trial in enumerate(groups[candidate]):
                points = trial.series.get(metric)
                if points:
                    traces.append(
                        (
                            f"{candidate} / {trial.run_id}",
                            colors[candidate],
                            _DASHES[index % len(_DASHES)],
                            points,
                        )
                    )
        if traces:
            safe_scenario = re.sub(r"[^A-Za-z0-9_-]+", "_", scenario)
            name = f"trials_{safe_scenario}_{metric}.svg"
            _chart(execution_dir / name, axis_label, traces)
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
            "Each bar spans a logged task start and finish. Time zero is the recorded load start, or the first task start when unavailable. Tasks missing either timestamp are omitted. Colors show completion or failure.",
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
            for path in execution_dir.glob("*/*/*/tasking.parquet")
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
            execution_dir, scenario, scenario_trials, colors
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
