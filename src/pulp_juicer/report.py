"""Render a Quarto report from completed load-test runs."""

import csv
import html
import itertools
import json
import math
import shutil
import subprocess
from collections import Counter
from datetime import datetime
from pathlib import Path

_REPORT_LAYOUT_MARKER = "<!-- pulp-juicer-report-layout: 15 -->"


def read_csv(path):
    with path.open(newline="") as source:
        return list(csv.DictReader(source))


def number(row, key, suffix="", digits=1):
    value = row.get(key, "")
    try:
        return f"{float(value):,.{digits}f}{suffix}"
    except (TypeError, ValueError):
        return value or "N/A"


def markdown_value(value):
    return html.escape(str(value)).replace("|", "&#124;").replace("\n", " ")


def run_configuration(metadata, history):
    fixture = metadata["fixture"]
    load = metadata["load"]
    lines = [
        "### Run configuration",
        "",
        "| Setting | Value |",
        "|:--|:--|",
        f"| Fixture COUNT (artifacts for candidate) | {fixture['count']} |",
        f"| Fixture ID requested at setup | {markdown_value(fixture['requested_fixture_id'])} |",
        f"| Payload bytes requested at setup | {fixture['requested_payload_bytes']} |",
        f"| Load profile | {markdown_value(load.get('profile', 'UnitaryImpulseLoad'))} |",
        f"| Concurrent users (configured) | {load['users']} |",
        f"| Peak active users (observed) | {max(int(row['User Count']) for row in history) if history else 'N/A'} |",
        *(
            [
                "| User steps | "
                + markdown_value(
                    "; ".join(
                        f"{step['at_seconds']}s: {step['users']} users"
                        for step in load["steps"]
                    )
                )
                + " |"
            ]
            if load.get("steps")
            else []
        ),
        f"| Maximum duration | {markdown_value(load['duration'])} |",
        f"| Wait between requests | {load['wait_seconds']} s |",
        f"| Idle before load | {load['idle_seconds']} s |",
        f"| Tail after load | {load['tail_seconds']} s |",
        f"| Distribution URL | {markdown_value(metadata['setup']['base_url'])} |",
        f"| Upstream URL | {markdown_value(metadata['upstream_url'])} |",
        f"| Docker Compose project | {markdown_value(metadata['project'])} |",
        *[
            f"| Pulp setting {markdown_value(name)} | {markdown_value(value)} |"
            for name, value in sorted(metadata.get("pulp_settings", {}).items())
        ],
        f"| Fixture SHA256 | {fixture['manifest_sha256']} |",
        "",
        "#### Software sources",
        "",
        "| Component | Source | Git commit or ref | Local changes |",
        "|:--|:--|:--|:--|",
    ]
    for name, source in metadata["sources"].items():
        dirty = source["dirty"]
        changes = "N/A" if dirty is None else "Yes" if dirty else "No"
        lines.append(
            f"| {markdown_value(name)} | {markdown_value(source['source'])} | "
            f"{markdown_value(source['revision'])} | {changes} |"
        )
    lines.append("")
    if fixture["requested_count"] != fixture["count"]:
        lines += [
            (
                f"The fixture already existed at setup: COUNT={fixture['requested_count']} was requested, "
                f"and {fixture['count']} artifacts were available for this candidate."
            ),
            "",
        ]
    return lines


def chart(
    path,
    title,
    y_label,
    points,
    color,
    minimum_y=1,
    overlay=None,
    overlay_label="Completed requests",
    request_starts=None,
    request_starts_axis="right",
):
    if not points:
        return False
    overlay = overlay or []
    request_starts = request_starts or []
    width, height = 900, 360
    separate_starts_axis = bool(
        overlay and request_starts and request_starts_axis == "right"
    )
    left = 86
    right = 170 if separate_starts_axis else 95 if overlay or request_starts else 24
    top, bottom = 45, 65
    plot_width = width - left - right
    plot_height = height - top - bottom
    x_max = max(
        1,
        max(x for x, _ in points),
        *(x for x, _ in overlay),
        *(x for x, _ in request_starts),
    )
    y_max = max(
        minimum_y,
        max(y for _, y in points) * 1.1,
        *(count * 1.1 for _, count in request_starts if request_starts_axis == "left"),
    )

    def xy(x, y):
        return left + plot_width * x / x_max, top + plot_height * (1 - y / y_max)

    grid = []
    for index in range(5):
        value = y_max * index / 4
        y = xy(0, value)[1]
        grid.append(
            f'<line x1="{left}" y1="{y:.1f}" x2="{width - right}" y2="{y:.1f}" stroke="#e5e7eb"/>'
            f'<text x="{left - 9}" y="{y + 4:.1f}" text-anchor="end">{value:g}</text>'
        )
    for index in range(5):
        value = x_max * index / 4
        x = xy(value, 0)[0]
        grid.append(
            f'<text x="{x:.1f}" y="{height - bottom + 19}" text-anchor="middle">{value:.0f}</text>'
        )

    right_svg = ""
    starts_svg = ""
    legend = [
        f'<line x1="{left}" y1="22" x2="{left + 24}" y2="22" stroke="{color}" stroke-width="3"/>',
        f'<text x="{left + 32}" y="26">{html.escape(title)}</text>',
    ]
    if overlay or request_starts:

        def right_axis(position, scale, label):
            labels = "".join(
                f'<text x="{position + 9}" y="{top + plot_height * (1 - fraction) + 4:.1f}">{scale * fraction:g}</text>'
                for fraction in (0, 0.5, 1)
            )
            title_x = position + (55 if separate_starts_axis else 81)
            return (
                f'<g font-family="sans-serif" font-size="12" fill="#64748b">{labels}</g>'
                f'<line x1="{position}" y1="{top}" x2="{position}" y2="{height - bottom}" stroke="#94a3b8"/>'
                f'<text x="{title_x}" y="{top + plot_height / 2:.1f}" '
                f'transform="rotate(90 {title_x} {top + plot_height / 2:.1f})" text-anchor="middle" '
                f'font-family="sans-serif" font-size="12" fill="#475569">{html.escape(label)}</text>'
            )

        overlay_max = max((1, *(value for _, value in overlay)))
        starts_max = max((1, *(count for _, count in request_starts)))
        if overlay:
            right_svg += right_axis(width - right, overlay_max, overlay_label)
        elif request_starts_axis == "right":
            right_svg += right_axis(width - right, starts_max, "New requests/s")
        if separate_starts_axis:
            right_svg += right_axis(width - 75, starts_max, "New requests/s")
        if request_starts:
            legend_x = left + (540 if overlay else 270)
            starts_scale = y_max if request_starts_axis == "left" else starts_max
            starts_coordinates = " ".join(
                f"{left + plot_width * x / x_max:.1f},{top + plot_height * (1 - count / starts_scale):.1f}"
                for x, count in request_starts
            )
            starts_svg = (
                f'<polyline points="{starts_coordinates}" fill="none" stroke="#7c3aed" '
                'stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>'
            )
            legend += [
                f'<line x1="{legend_x}" y1="22" x2="{legend_x + 24}" y2="22" stroke="#7c3aed" stroke-width="2"/>',
                f'<text x="{legend_x + 32}" y="26">New requests/s ({request_starts_axis})</text>',
            ]
        if overlay:
            load_coordinates = " ".join(
                f"{left + plot_width * x / x_max:.1f},{top + plot_height * (1 - value / overlay_max):.1f}"
                for x, value in overlay
            )
            legend += [
                (
                    f'<line x1="{left + 270}" y1="22" x2="{left + 294}" y2="22" '
                    'stroke="#64748b" stroke-width="2" stroke-dasharray="5 5"/>'
                ),
                f'<text x="{left + 302}" y="26">{html.escape(overlay_label)} (right)</text>',
            ]
            right_svg += (
                f'<polyline points="{load_coordinates}" fill="none" stroke="#64748b" '
                'stroke-width="2" stroke-dasharray="5 5" opacity="0.55"/>'
            )

    coordinates = " ".join(
        f"{x:.1f},{y:.1f}" for x, y in (xy(*point) for point in points)
    )
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'role="img" aria-label="{html.escape(title, quote=True)}">'
        '<rect width="100%" height="100%" fill="#fff"/>'
        '<g font-family="sans-serif" font-size="12" fill="#475569">'
        + "".join(legend)
        + "</g>"
        '<g font-family="sans-serif" font-size="12" fill="#475569">'
        + "".join(grid)
        + "</g>"
        + f'<line x1="{left}" y1="{top}" x2="{left}" y2="{height - bottom}" stroke="#94a3b8"/>'
        + f'<line x1="{left}" y1="{height - bottom}" x2="{width - right}" y2="{height - bottom}" stroke="#94a3b8"/>'
        + f'<polyline points="{coordinates}" fill="none" stroke="{color}" stroke-width="3" '
        'stroke-linecap="round" stroke-linejoin="round"/>'
        + right_svg
        + starts_svg
        + f'<text x="{left + plot_width / 2:.1f}" y="{height - 9}" text-anchor="middle" '
        'font-family="sans-serif" font-size="12" fill="#475569">Time since recording start (s)</text>'
        + f'<text x="20" y="{top + plot_height / 2:.1f}" '
        f'transform="rotate(-90 20 {top + plot_height / 2:.1f})" text-anchor="middle" '
        f'font-family="sans-serif" font-size="12" fill="#475569">{html.escape(y_label)}</text></svg>\n'
    )
    path.write_text(svg)
    return True


def request_starts_per_second(rows, timeline_start, timeline_end):
    counts = Counter(
        max(
            0,
            math.floor(
                datetime.fromisoformat(row["started_at_utc"]).timestamp()
                - timeline_start
            ),
        )
        for row in rows
    )
    end = max(timeline_end, max(counts, default=0) + 1)
    return [(second, counts[second]) for second in range(end + 1)]


def prepare_run(run_dir, results_dir):
    from .tasking import task_report_sections

    candidate, run_id = run_dir.parent.name, run_dir.name
    scenario = (
        run_dir.parent.parent.name if run_dir.parent.parent != results_dir else None
    )

    def asset(name):
        return f"{run_dir.relative_to(results_dir).as_posix()}/{name}"

    metadata_path = run_dir / "run_metadata.json"
    metadata = (
        json.loads(metadata_path.read_text()) if metadata_path.is_file() else None
    )
    history_path = run_dir / "locust_stats_history.csv"
    history = (
        [row for row in read_csv(history_path) if row["Name"] == "Aggregated"]
        if history_path.is_file()
        else []
    )
    stats_rows = read_csv(run_dir / "locust_stats.csv")
    stats = next((row for row in stats_rows if row["Name"] == "Aggregated"), None)
    if stats is None:
        raise ValueError(f"No aggregated Locust stats in {run_dir}")

    lines = [
        f"Candidate: **{candidate}**. "
        + (f"Scenario: **{scenario}**. " if scenario else "")
        + f"Run: **{run_id}**.",
        "",
        "### Summary",
        "",
        "#### Requests",
        "",
        "| Metric | Value |",
        "|:--|--:|",
        f"| Completed requests | {stats['Request Count']} |",
        f"| Failures | {stats['Failure Count']} |",
        f"| Requests/s | {number(stats, 'Requests/s', digits=2)} |",
        f"| Mean response time | {number(stats, 'Average Response Time', ' ms')} |",
        f"| p50 response time | {number(stats, '50%', ' ms')} |",
        f"| p95 response time | {number(stats, '95%', ' ms')} |",
        f"| p99 response time | {number(stats, '99%', ' ms')} |",
        "",
        "Response time covers the HTTP request; later autocommit is excluded.",
        "",
    ]
    timeline = ["### Timeline", ""]
    completed_timestamps = []
    starts = None
    start = None

    if history_path.is_file() and history:
        start = int(history[0]["Timestamp"])
        completed_timestamps = [
            (int(row["Timestamp"]), int(row["Total Request Count"])) for row in history
        ]
        completed = [
            (timestamp - start, count) for timestamp, count in completed_timestamps
        ]
        throughput = [(completed[0][0], 0)]
        for (previous_time, previous_count), (now, count) in itertools.pairwise(
            completed
        ):
            throughput.append(
                (now, (count - previous_count) / max(1, now - previous_time))
            )
        requests_path = run_dir / "requests.csv"
        if requests_path.is_file():
            with requests_path.open(newline="") as source:
                starts = request_starts_per_second(
                    csv.DictReader(source),
                    start,
                    int(history[-1]["Timestamp"]) - start,
                )
        chart(
            run_dir / "report_completed.svg",
            "Completed requests",
            "Completed requests (count)",
            completed,
            "#2563eb",
            request_starts=starts,
        )
        chart(
            run_dir / "report_throughput.svg",
            "Completed requests per second",
            "Throughput (requests/s)",
            throughput,
            "#e56b35",
            overlay=completed,
            overlay_label="Completed requests",
            request_starts=starts,
            request_starts_axis="left",
        )
        caption = "The throughput chart overlays cumulative completed requests. Dashed gray overlays use the right axes. Idle and tail periods remain visible."
        if starts is not None:
            caption = (
                "Purple lines show new requests per second on the completed-requests and throughput charts. "
                + caption
            )
        timeline += [
            "#### Requests",
            "",
            caption,
            "",
            f"![Completed requests]({asset('report_completed.svg')})",
            "",
            f"![Completed requests per second]({asset('report_throughput.svg')})",
            "",
        ]

    cpu_path = run_dir / "db_cpu.csv"
    summary_path = run_dir / "db_cpu_summary.csv"
    if cpu_path.is_file() and summary_path.is_file():
        cpu = read_csv(cpu_path)
        summary = next(iter(read_csv(summary_path)), None)
        if summary:
            lines += [
                "#### PostgreSQL CPU",
                "",
                "100% means one fully used CPU core. The measured period is shown in the summary CSV.",
                "",
                "| Metric | Value |",
                "|:--|--:|",
                f"| Mean CPU | {number(summary, 'postgres_cpu_mean_pct', '%', 2)} |",
                f"| Peak CPU | {number(summary, 'postgres_cpu_peak_pct', '%', 2)} |",
                f"| CPU time | {number(summary, 'postgres_cpu_seconds', ' core-seconds', 2)} |",
                f"| Duration | {number(summary, 'duration_s', ' s', 2)} |",
                f"| Samples | {summary['samples']} |",
                "",
            ]
            if cpu:
                samples = [
                    (float(row["elapsed_s"]), float(row["postgres_cpu_pct"]))
                    for row in cpu
                ]
                cpu_completed = []
                cpu_starts = []
                if completed_timestamps and cpu[0].get("timestamp_utc"):
                    cpu_start = (
                        datetime.fromisoformat(cpu[0]["timestamp_utc"]).timestamp()
                        - samples[0][0]
                    )
                    cpu_completed = [
                        (max(0, timestamp - cpu_start), count)
                        for timestamp, count in completed_timestamps
                    ]
                    if starts is not None and start is not None:
                        cpu_starts = [
                            (start + second - cpu_start, count)
                            for second, count in starts
                            if start + second >= cpu_start
                        ]
                chart(
                    run_dir / "report_db_cpu.svg",
                    "PostgreSQL CPU",
                    "PostgreSQL CPU (% of one core)",
                    samples,
                    "#16a34a",
                    100,
                    overlay=cpu_completed,
                    overlay_label="Completed requests",
                    request_starts=cpu_starts,
                )
                timeline += ["#### PostgreSQL CPU", ""]
                if cpu_completed:
                    timeline += [
                        "The dashed gray curve shows cumulative completed requests. The purple line shows new requests per second. Both use their labeled right axes.",
                        "",
                    ]
                timeline += [f"![PostgreSQL CPU]({asset('report_db_cpu.svg')})", ""]

    task_summary, task_distribution, task_timeline = task_report_sections(
        run_dir, results_dir
    )
    lines += task_summary
    details = []
    if metadata:
        details += run_configuration(metadata, history)
    details += ["### Source files", ""]
    for name, label in (
        ("run_metadata.json", "Run configuration, setup, sources, and result"),
        ("pulp_status.json", "Pulp status after the load test"),
        ("pulp_status_error.txt", "Pulp status capture error"),
        ("compose.log", "Compose service logs"),
        ("tasking.parquet", "Structured Pulp task records"),
        ("data_load.csv", "Requests selected for this run"),
        ("locust_stats.csv", "Locust summary CSV"),
        ("locust_stats_history.csv", "Locust history CSV"),
        ("requests.csv", "Individual requests CSV"),
        ("db_cpu.csv", "PostgreSQL CPU samples"),
        ("db_cpu_summary.csv", "PostgreSQL CPU summary"),
    ):
        if (run_dir / name).is_file():
            details.append(f"- [{label}]({asset(name)})")
    details.append("")
    timeline += task_timeline
    if len(timeline) == 2:
        timeline += ["No timeline data available.", ""]
    graphs = (
        ["### Wait-time distribution", "", *task_distribution]
        if task_distribution
        else []
    )
    graphs += timeline

    return stats, lines, graphs, details


def completed_runs(execution_dir):
    if not execution_dir.is_dir():
        return
    stats_files = execution_dir.glob("*/*/*/locust_stats.csv")
    for run_dir in sorted(stats_files):
        run_dir = run_dir.parent
        relative = run_dir.relative_to(execution_dir)
        if any(part.startswith("_") for part in relative.parts):
            continue
        yield run_dir


def render_report(
    execution_dir: Path,
    quarto_command: str = "quarto",
    title: str | None = None,
) -> Path | None:
    """Render one execution, reusing its existing HTML report when present."""
    execution_dir = Path(execution_dir)
    report = execution_dir / "report.html"
    if report.is_file():
        report_time = report.stat().st_mtime_ns
        tasking_files = list(execution_dir.glob("*/*/*/tasking.parquet"))
        source = execution_dir / "report.qmd"
        source_text = source.read_text() if source.is_file() else ""
        generated_source = (
            "# Runs" in source_text or "<!-- pulp-juicer-report-layout:" in source_text
        )
        layout_stale = (generated_source or bool(tasking_files)) and (
            not source_text or _REPORT_LAYOUT_MARKER not in source_text
        )
        data_stale = any(
            path.stat().st_mtime_ns > report_time for path in tasking_files
        )
        if not layout_stale and not data_stale:
            return report.resolve()
    runs = list(completed_runs(execution_dir))
    if not runs:
        return None
    quarto = shutil.which(quarto_command)
    if quarto is None:
        raise FileNotFoundError(
            f"Quarto executable not found: {quarto_command}. "
            "Install Quarto or run `make setup` to install the report dependencies."
        )
    quarto = str(Path(quarto).resolve())
    runs.sort(key=lambda run_dir: (run_dir.name, run_dir.parent.name))
    lines = [
        "---",
        f"title: {json.dumps(title or f'Pulp Juicer load test: {execution_dir.name}')}",
        "format:",
        "  html:",
        "    toc: true",
        "    embed-resources: true",
        "---",
        "",
        _REPORT_LAYOUT_MARKER,
        "",
        "# Overview",
        "",
        "| Execution | Scenario | Run | Candidate | Requests | Failures | Mean response (ms) | p95 (ms) |",
        "|:--|:--|:--|:--|--:|--:|--:|--:|",
    ]
    summaries = []
    graphs = []
    details = []
    for run_dir in runs:
        stats, summary, run_graphs, run_details = prepare_run(run_dir, execution_dir)
        execution = execution_dir.name
        scenario = run_dir.parent.parent.name
        lines.append(
            f"| {execution} | {scenario} | {run_dir.name} | {run_dir.parent.name} | {stats['Request Count']} | "
            f"{stats['Failure Count']} | {number(stats, 'Average Response Time')} | "
            f"{number(stats, '95%')} |"
        )
        heading = f"{execution} / {scenario} / "
        run_heading = f"## {heading}{run_dir.parent.name} / {run_dir.name}"
        summaries.extend([run_heading, "", *summary])
        graphs.extend([run_heading, "", *run_graphs])
        details.extend([run_heading, "", *run_details])
    for section_title, sections in (
        ("Summaries", summaries),
        ("Graphs", graphs),
        ("Run configuration and source files", details),
    ):
        lines.extend(
            [
                "",
                f"# {section_title}",
                "",
                "::: {.panel-tabset}",
                "",
                *sections,
                ":::",
                "",
            ]
        )
    source = execution_dir / "report.qmd"
    source.write_text("\n".join(lines))
    subprocess.run(
        [
            quarto,
            "render",
            source.name,
            "--to",
            "html",
            "--output",
            "report.html",
            "--quiet",
        ],
        cwd=execution_dir,
        check=True,
    )
    for run_dir in runs:
        for name in ("report.html", "report.qmd"):
            (run_dir / name).unlink(missing_ok=True)
    return report.resolve()
