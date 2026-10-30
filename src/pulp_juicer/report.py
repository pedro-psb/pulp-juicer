"""Render a Quarto report from completed load-test runs."""

import csv
import html
import json
import math
import shutil
import subprocess
from collections import Counter
from datetime import datetime
from pathlib import Path


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
        f'| Fixture COUNT (artifacts for candidate) | {fixture["count"]} |',
        f'| Fixture ID requested at setup | {markdown_value(fixture["requested_fixture_id"])} |',
        f'| Payload bytes requested at setup | {fixture["requested_payload_bytes"]} |',
        f'| Concurrent users (configured) | {load["users"]} |',
        f'| Peak active users (observed) | {max(int(row["User Count"]) for row in history) if history else "N/A"} |',
        f'| Spawn rate | {load["spawn_rate"]} users/s |',
        f'| Maximum duration | {markdown_value(load["duration"])} |',
        f'| Wait between requests | {load["wait_seconds"]} s |',
        f'| Idle before load | {load["idle_seconds"]} s |',
        f'| Tail after load | {load["tail_seconds"]} s |',
        f'| Distribution URL | {markdown_value(metadata["setup"]["base_url"])} |',
        f'| Upstream URL | {markdown_value(metadata["upstream_url"])} |',
        f'| Docker Compose project | {markdown_value(metadata["project"])} |',
        f'| Fixture SHA256 | {fixture["manifest_sha256"]} |',
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
            f'| {markdown_value(name)} | {markdown_value(source["source"])} | '
            f'{markdown_value(source["revision"])} | {changes} |'
        )
    lines.append("")
    if fixture["requested_count"] != fixture["count"]:
        lines += [
            f'The fixture already existed at setup: COUNT={fixture["requested_count"]} was requested, '
            f'and {fixture["count"]} artifacts were available for this candidate.',
            "",
        ]
    return lines


def chart(path, title, y_label, points, color, minimum_y=1, overlay=None, overlay_label="Completed requests", request_starts=None):
    if not points:
        return False
    overlay = overlay or []
    request_starts = request_starts or []
    width, height = 900, 360
    left, right, top, bottom = 86, 95 if overlay or request_starts else 24, 45, 65
    plot_width = width - left - right
    plot_height = height - top - bottom
    x_max = max(1, max(x for x, _ in points), *(x for x, _ in overlay), *(x for x, _ in request_starts))
    y_max = max(minimum_y, max(y for _, y in points) * 1.1)

    def xy(x, y):
        return left + plot_width * x / x_max, top + plot_height * (1 - y / y_max)

    grid = []
    for index in range(5):
        value = y_max * index / 4
        y = xy(0, value)[1]
        grid.append(
            f'<line x1="{left}" y1="{y:.1f}" x2="{width-right}" y2="{y:.1f}" stroke="#e5e7eb"/>'
            f'<text x="{left-9}" y="{y+4:.1f}" text-anchor="end">{value:g}</text>'
        )
    for index in range(5):
        value = x_max * index / 4
        x = xy(value, 0)[0]
        grid.append(f'<text x="{x:.1f}" y="{height-bottom+19}" text-anchor="middle">{value:.0f}</text>')

    right_svg = ""
    bars_svg = ""
    legend = [
        f'<line x1="{left}" y1="22" x2="{left+24}" y2="22" stroke="{color}" stroke-width="3"/>',
        f'<text x="{left+32}" y="26">{html.escape(title)}</text>',
    ]
    if overlay or request_starts:
        right_max = max(1, *(value for _, value in overlay), *(count for _, count in request_starts))
        labels = "".join(
            f'<text x="{width-right+9}" y="{top + plot_height * (1 - fraction) + 4:.1f}">{right_max * fraction:g}</text>'
            for fraction in (0, 0.5, 1)
        )
        right_svg = f'<g font-family="sans-serif" font-size="12" fill="#64748b">{labels}</g>'
        right_label = overlay_label if overlay else "New requests per second"
        if overlay and request_starts:
            right_label += " / new requests per second"
        right_svg += (
            f'<line x1="{width-right}" y1="{top}" x2="{width-right}" y2="{height-bottom}" stroke="#94a3b8"/>'
            f'<text x="{width-14}" y="{top+plot_height/2:.1f}" '
            f'transform="rotate(90 {width-14} {top+plot_height/2:.1f})" text-anchor="middle" '
            f'font-family="sans-serif" font-size="12" fill="#475569">{html.escape(right_label)}</text>'
        )
        if request_starts:
            bar_width = plot_width / x_max
            legend_x = left + (540 if overlay else 270)
            bars_svg = "".join(
                f'<rect x="{xy(x, 0)[0]:.1f}" y="{top + plot_height * (1 - count / right_max):.1f}" '
                f'width="{bar_width * 0.85:.1f}" height="{plot_height * count / right_max:.1f}" '
                'fill="#7c3aed" opacity="0.7"/>'
                for x, count in request_starts if count
            )
            legend += [
                f'<rect x="{legend_x}" y="16" width="12" height="12" fill="#7c3aed" opacity="0.7"/>',
                f'<text x="{legend_x+20}" y="26">New requests per second (right)</text>',
            ]
        if overlay:
            load_coordinates = " ".join(
                f"{left + plot_width * x / x_max:.1f},{top + plot_height * (1 - value / right_max):.1f}"
                for x, value in overlay
            )
            legend += [
                f'<line x1="{left+270}" y1="22" x2="{left+294}" y2="22" '
                'stroke="#64748b" stroke-width="2" stroke-dasharray="5 5"/>',
                f'<text x="{left+302}" y="26">{html.escape(overlay_label)} (right)</text>',
            ]
            right_svg += (
                f'<polyline points="{load_coordinates}" fill="none" stroke="#64748b" '
                'stroke-width="2" stroke-dasharray="5 5" opacity="0.55"/>'
            )

    coordinates = " ".join(f"{x:.1f},{y:.1f}" for x, y in (xy(*point) for point in points))
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'role="img" aria-label="{html.escape(title, quote=True)}">'
        '<rect width="100%" height="100%" fill="#fff"/>'
        '<g font-family="sans-serif" font-size="12" fill="#475569">'
        + "".join(legend) + '</g>'
        '<g font-family="sans-serif" font-size="12" fill="#475569">'
        + "".join(grid)
        + '</g>'
        + f'<line x1="{left}" y1="{top}" x2="{left}" y2="{height-bottom}" stroke="#94a3b8"/>'
        + f'<line x1="{left}" y1="{height-bottom}" x2="{width-right}" y2="{height-bottom}" stroke="#94a3b8"/>'
        + bars_svg
        + f'<polyline points="{coordinates}" fill="none" stroke="{color}" stroke-width="3" '
        'stroke-linecap="round" stroke-linejoin="round"/>'
        + right_svg
        + f'<text x="{left+plot_width/2:.1f}" y="{height-9}" text-anchor="middle" '
        'font-family="sans-serif" font-size="12" fill="#475569">Time since recording start (s)</text>'
        + f'<text x="20" y="{top+plot_height/2:.1f}" '
        f'transform="rotate(-90 20 {top+plot_height/2:.1f})" text-anchor="middle" '
        f'font-family="sans-serif" font-size="12" fill="#475569">{html.escape(y_label)}</text></svg>\n'
    )
    path.write_text(svg)
    return True


def request_starts_per_second(rows, timeline_start, timeline_end):
    counts = Counter(
        max(0, math.floor(datetime.fromisoformat(row["started_at_utc"]).timestamp() - timeline_start))
        for row in rows
    )
    end = max(timeline_end, max(counts, default=0) + 1)
    return [(second, counts[second]) for second in range(end + 1)]


def prepare_run(run_dir, results_dir):
    candidate, run_id = run_dir.parent.name, run_dir.name
    scenario = run_dir.parent.parent.name if run_dir.parent.parent != results_dir else None
    def asset(name):
        return f"{run_dir.relative_to(results_dir).as_posix()}/{name}"
    metadata_path = run_dir / "run_metadata.json"
    metadata = json.loads(metadata_path.read_text()) if metadata_path.is_file() else None
    history_path = run_dir / "locust_stats_history.csv"
    history = [row for row in read_csv(history_path) if row["Name"] == "Aggregated"] if history_path.is_file() else []
    stats_rows = read_csv(run_dir / "locust_stats.csv")
    stats = next((row for row in stats_rows if row["Name"] == "Aggregated"), None)
    if stats is None:
        raise ValueError(f"No aggregated Locust stats in {run_dir}")

    lines = [
        f"Candidate: **{candidate}**. " + (f"Scenario: **{scenario}**. " if scenario else "") + f"Run: **{run_id}**.",
        "",
        "### Summary",
        "",
        "#### Requests",
        "",
        "| Metric | Value |",
        "|:--|--:|",
        f'| Completed requests | {stats["Request Count"]} |',
        f'| Failures | {stats["Failure Count"]} |',
        f'| Requests/s | {number(stats, "Requests/s", digits=2)} |',
        f'| Mean response time | {number(stats, "Average Response Time", " ms")} |',
        f'| p50 response time | {number(stats, "50%", " ms")} |',
        f'| p95 response time | {number(stats, "95%", " ms")} |',
        f'| p99 response time | {number(stats, "99%", " ms")} |',
        "",
        "Response time covers the HTTP request; later autocommit is excluded.",
        "",
    ]
    timeline = ["### Timeline", ""]
    completed_timestamps = []

    if history_path.is_file():
        if history:
            start = int(history[0]["Timestamp"])
            completed_timestamps = [(int(row["Timestamp"]), int(row["Total Request Count"])) for row in history]
            completed = [(timestamp - start, count) for timestamp, count in completed_timestamps]
            throughput = [(completed[0][0], 0)]
            for (previous_time, previous_count), (now, count) in zip(completed, completed[1:]):
                throughput.append((now, (count - previous_count) / max(1, now - previous_time)))
            requests_path = run_dir / "requests.csv"
            starts = None
            if requests_path.is_file():
                with requests_path.open(newline="") as source:
                    starts = request_starts_per_second(
                        csv.DictReader(source), start, int(history[-1]["Timestamp"]) - start
                    )
            chart(
                run_dir / "report_completed.svg", "Completed requests", "Completed requests (count)", completed,
                "#2563eb", request_starts=starts,
            )
            chart(
                run_dir / "report_throughput.svg", "Completed requests per second", "Throughput (requests/s)",
                throughput, "#e56b35", overlay=completed, overlay_label="Completed requests",
            )
            caption = "The throughput chart overlays cumulative completed requests. Dashed gray overlays use the right axes. Idle and tail periods remain visible."
            if starts is not None:
                caption = "Purple bars on the completed-requests chart show new requests per second. " + caption
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
                f'| Mean CPU | {number(summary, "postgres_cpu_mean_pct", "%", 2)} |',
                f'| Peak CPU | {number(summary, "postgres_cpu_peak_pct", "%", 2)} |',
                f'| CPU time | {number(summary, "postgres_cpu_seconds", " core-seconds", 2)} |',
                f'| Duration | {number(summary, "duration_s", " s", 2)} |',
                f'| Samples | {summary["samples"]} |',
                "",
            ]
            if cpu:
                samples = [(float(row["elapsed_s"]), float(row["postgres_cpu_pct"])) for row in cpu]
                cpu_completed = []
                if completed_timestamps and cpu[0].get("timestamp_utc"):
                    cpu_start = datetime.fromisoformat(cpu[0]["timestamp_utc"]).timestamp() - samples[0][0]
                    cpu_completed = [(max(0, timestamp - cpu_start), count) for timestamp, count in completed_timestamps]
                chart(
                    run_dir / "report_db_cpu.svg", "PostgreSQL CPU", "PostgreSQL CPU (% of one core)",
                    samples, "#16a34a", 100, overlay=cpu_completed, overlay_label="Completed requests",
                )
                timeline += ["#### PostgreSQL CPU", ""]
                if cpu_completed:
                    timeline += ["The dashed gray curve shows cumulative completed requests (right axis).", ""]
                timeline += [f"![PostgreSQL CPU]({asset('report_db_cpu.svg')})", ""]

    if len(timeline) == 2:
        timeline += ["No timeline data available.", ""]
    lines += timeline
    lines += ["### Source files", ""]
    for name, label in (
        ("run_metadata.json", "Run configuration, setup, sources, and result"),
        ("data_load.csv", "Requests selected for this run"),
        ("locust_stats.csv", "Locust summary CSV"),
        ("locust_stats_history.csv", "Locust history CSV"),
        ("requests.csv", "Individual requests CSV"),
        ("db_cpu.csv", "PostgreSQL CPU samples"),
        ("db_cpu_summary.csv", "PostgreSQL CPU summary"),
    ):
        if (run_dir / name).is_file():
            lines.append(f"- [{label}]({asset(name)})")
    lines.append("")
    if metadata:
        lines += run_configuration(metadata, history)

    return stats, lines


def completed_runs(results):
    if not results.is_dir():
        return
    stats_files = results.glob("*/*/*/locust_stats.csv")
    for run_dir in sorted(stats_files):
        run_dir = run_dir.parent
        relative = run_dir.relative_to(results)
        if any(part.startswith("_") for part in relative.parts):
            continue
        yield run_dir


def render_report(
    results_dir: Path = Path("_juice/results"),
    quarto_command: str = "quarto",
    title: str = "Pulp Juicer load test",
) -> Path | None:
    """Render completed runs and return the HTML path, or None when there are no runs."""
    results_dir = Path(results_dir)
    runs = list(completed_runs(results_dir))
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
        f"title: {json.dumps(title)}",
        "format:",
        "  html:",
        "    toc: true",
        "    embed-resources: true",
        "---",
        "",
        "# Overview",
        "",
        "| Scenario | Run | Candidate | Requests | Failures | Mean response (ms) | p95 (ms) |",
        "|:--|:--|:--|--:|--:|--:|--:|",
    ]
    sections = []
    for run_dir in runs:
        stats, body = prepare_run(run_dir, results_dir)
        scenario = run_dir.parent.parent.name if len(run_dir.relative_to(results_dir).parts) == 3 else ""
        lines.append(
            f"| {scenario} | {run_dir.name} | {run_dir.parent.name} | {stats['Request Count']} | "
            f"{stats['Failure Count']} | {number(stats, 'Average Response Time')} | "
            f"{number(stats, '95%')} |"
        )
        heading = f"{scenario} / " if scenario else ""
        sections.extend([f"## {heading}{run_dir.parent.name} / {run_dir.name}", "", *body])
    lines.extend(["", "# Runs", "", "::: {.panel-tabset}", "", *sections, ":::", ""])
    source = results_dir / "report.qmd"
    source.write_text("\n".join(lines))
    subprocess.run([quarto, "render", source.name, "--to", "html", "--output", "report.html", "--quiet"], cwd=results_dir, check=True)
    for run_dir in runs:
        for name in ("report.html", "report.qmd"):
            (run_dir / name).unlink(missing_ok=True)
    return (results_dir / "report.html").resolve()


