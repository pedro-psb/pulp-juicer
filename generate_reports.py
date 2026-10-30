"""Render one Quarto report containing all completed candidate runs."""

import argparse
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


def chart(path, title, y_label, points, color, minimum_y=1, overlay=None, overlay_label="Active users", request_starts=None):
    if not points:
        return False
    overlay = overlay or []
    request_starts = request_starts or []
    width, height = 900, 340
    left, right, top, bottom = 72, 68 if overlay or request_starts else 24, 42, 52
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
        grid.append(f'<text x="{x:.1f}" y="{height-24}" text-anchor="middle">{value:.0f}s</text>')

    right_svg = ""
    bars_svg = ""
    if overlay or request_starts:
        right_max = max(1, *(value for _, value in overlay), *(count for _, count in request_starts))
        labels = "".join(
            f'<text x="{width-right+9}" y="{top + plot_height * (1 - fraction) + 4:.1f}">{right_max * fraction:g}</text>'
            for fraction in (0, 0.5, 1)
        )
        right_svg = f'<g font-family="sans-serif" font-size="12" fill="#64748b">{labels}</g>'
        if request_starts:
            bar_width = plot_width / x_max
            bars_svg = "".join(
                f'<rect x="{xy(x, 0)[0]:.1f}" y="{top + plot_height * (1 - count / right_max):.1f}" '
                f'width="{bar_width * 0.85:.1f}" height="{plot_height * count / right_max:.1f}" '
                'fill="#7c3aed" opacity="0.7"/>'
                for x, count in request_starts if count
            )
            right_svg += (
                f'<rect x="{width-right-290}" y="15" width="11" height="11" fill="#7c3aed" opacity="0.7"/>'
                f'<text x="{width-right-273}" y="26" font-family="sans-serif" font-size="12" fill="#64748b">Started/s</text>'
            )
        if overlay:
            stepped = []
            for x, value in overlay:
                if stepped:
                    stepped.append((x, stepped[-1][1]))
                stepped.append((x, value))
            load_coordinates = " ".join(
                f"{left + plot_width * x / x_max:.1f},{top + plot_height * (1 - value / right_max):.1f}"
                for x, value in stepped
            )
            right_svg += (
                f'<line x1="{width-right-183}" y1="22" x2="{width-right-159}" y2="22" '
                'stroke="#64748b" stroke-width="2" stroke-dasharray="5 5" opacity="0.65"/>'
                f'<text x="{width-right-152}" y="26" font-family="sans-serif" font-size="12" fill="#64748b">{html.escape(overlay_label)} (right axis)</text>'
                f'<polyline points="{load_coordinates}" fill="none" stroke="#64748b" '
                'stroke-width="2" stroke-dasharray="5 5" opacity="0.55"/>'
            )

    coordinates = " ".join(f"{x:.1f},{y:.1f}" for x, y in (xy(*point) for point in points))
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'role="img" aria-label="{html.escape(title, quote=True)}">'
        '<rect width="100%" height="100%" fill="#fff"/>'
        f'<text x="{left}" y="27" font-family="sans-serif" font-size="19" fill="#172033">{html.escape(title)}</text>'
        '<g font-family="sans-serif" font-size="12" fill="#475569">'
        + "".join(grid)
        + '</g>' + bars_svg
        + f'<polyline points="{coordinates}" fill="none" stroke="{color}" stroke-width="3" '
        'stroke-linecap="round" stroke-linejoin="round"/>'
        + right_svg
        + f'<text x="{left}" y="{height-4}" font-family="sans-serif" font-size="12" fill="#475569">'
        f'Seconds from recording start · {html.escape(y_label)}</text></svg>\n'
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
        f"Candidate: **{candidate}**. Run: **{run_id}**.",
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
    if metadata:
        fixture = metadata["fixture"]
        load = metadata["load"]
        lines += [
            "#### Run configuration",
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
    timeline = ["### Timeline", ""]
    completed_timestamps = []

    if history_path.is_file():
        if history:
            start = int(history[0]["Timestamp"])
            completed_timestamps = [(int(row["Timestamp"]), int(row["Total Request Count"])) for row in history]
            completed = [(timestamp - start, count) for timestamp, count in completed_timestamps]
            user_load = [(int(row["Timestamp"]) - start, int(row["User Count"])) for row in history]
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
                run_dir / "report_completed.svg", "Completed requests", "requests", completed,
                "#2563eb", overlay=user_load, request_starts=starts,
            )
            chart(
                run_dir / "report_throughput.svg", "Completed requests per second", "requests/s",
                throughput, "#e56b35", overlay=completed, overlay_label="Completed",
            )
            caption = "The completed-requests chart overlays active users; the throughput chart overlays cumulative completed requests. Dashed gray curves use the right axes. Idle and tail periods remain visible."
            if starts is not None:
                caption = "Purple bars on the completed-requests chart count starts per second. " + caption
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
                    run_dir / "report_db_cpu.svg", "PostgreSQL CPU", "% of one core",
                    samples, "#16a34a", 100, overlay=cpu_completed, overlay_label="Completed",
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
        ("run_metadata.json", "Run configuration and source commits or refs"),
        ("manifest.json", "Fixture manifest used for this run"),
        ("setup.json", "Pulp repository and distribution setup"),
        ("locust_stats.csv", "Locust summary CSV"),
        ("locust_stats_history.csv", "Locust history CSV"),
        ("requests.csv", "Individual requests CSV"),
        ("db_cpu.csv", "PostgreSQL CPU samples"),
        ("db_cpu_summary.csv", "PostgreSQL CPU summary"),
    ):
        if (run_dir / name).is_file():
            lines.append(f"- [{label}]({asset(name)})")
    lines.append("")

    return stats, lines


def completed_runs(results):
    if not results.is_dir():
        return
    for candidate_dir in sorted(results.iterdir()):
        if not candidate_dir.is_dir() or candidate_dir.name.startswith("_"):
            continue
        for run_dir in sorted(candidate_dir.iterdir()):
            if run_dir.is_dir() and (run_dir / "locust_stats.csv").is_file():
                yield run_dir


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, default=Path("_results"))
    parser.add_argument("--quarto", default="quarto")
    args = parser.parse_args()
    runs = list(completed_runs(args.results))
    if not runs:
        print(f"No completed runs found under {args.results}")
        return
    quarto = shutil.which(args.quarto)
    if quarto is None:
        parser.error(f"Quarto executable not found: {args.quarto}. Install Quarto or set QUARTO=/path/to/quarto")
    quarto = str(Path(quarto).resolve())
    runs.sort(key=lambda run_dir: (run_dir.name, run_dir.parent.name))
    lines = [
        "---",
        f"title: {json.dumps('Maven pull-through load test')}",
        "format:",
        "  html:",
        "    toc: true",
        "    embed-resources: true",
        "---",
        "",
        "# Overview",
        "",
        "| Run | Candidate | Requests | Failures | Mean response (ms) | p95 (ms) |",
        "|:--|:--|--:|--:|--:|--:|",
    ]
    sections = []
    for run_dir in runs:
        stats, body = prepare_run(run_dir, args.results)
        lines.append(
            f"| {run_dir.name} | {run_dir.parent.name} | {stats['Request Count']} | "
            f"{stats['Failure Count']} | {number(stats, 'Average Response Time')} | "
            f"{number(stats, '95%')} |"
        )
        sections.extend([f"## {run_dir.parent.name} / {run_dir.name}", "", *body])
    lines.extend(["", "# Runs", "", "::: {.panel-tabset}", "", *sections, ":::", ""])
    source = args.results / "report.qmd"
    source.write_text("\n".join(lines))
    subprocess.run([quarto, "render", source.name, "--to", "html", "--output", "report.html", "--quiet"], cwd=args.results, check=True)
    for run_dir in runs:
        for name in ("report.html", "report.qmd"):
            (run_dir / name).unlink(missing_ok=True)
    print(f"Report: {(args.results / 'report.html').resolve()}")


if __name__ == "__main__":
    main()
