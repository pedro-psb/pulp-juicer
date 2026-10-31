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

from .report import completed_runs, markdown_value, read_csv

_COLORS = ("#2563eb", "#e56b35", "#16a34a", "#a855f7", "#dc2626", "#0891b2")
_DASHES = ("", "8 4", "3 3", "11 4 3 4")
_METRICS = (
    ("completed", "Cumulative completed requests", "Completed requests (count)"),
    ("throughput", "Completed requests per second", "Completed requests/s"),
    ("starts", "New requests per second", "New requests/s"),
    ("cpu", "PostgreSQL CPU", "PostgreSQL CPU (% of one core)"),
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
        series=_timeline(run_dir, metadata),
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


def _scenario_section(execution_dir, scenario, trials, colors):
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
    lines += [
        "",
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
            lines += [f"#### {title}", "", f"![{title}]({name})", ""]
    if not any(trial.series for trial in trials):
        lines += ["No request timelines available.", ""]
    return lines


def render_trial_report(
    execution_dir: Path, quarto_command: str = "quarto"
) -> Path | None:
    """Render run-level summaries and candidate comparisons for one execution."""
    execution_dir = Path(execution_dir)
    report = execution_dir / "trials.html"
    if report.is_file():
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
    ]
    for scenario, scenario_trials in sorted(groups.items()):
        scenario_trials.sort(key=lambda trial: trial.run_id)
        lines.extend(
            _scenario_section(execution_dir, scenario, scenario_trials, colors)
        )
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
