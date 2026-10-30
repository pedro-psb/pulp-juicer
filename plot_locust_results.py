#!/usr/bin/env python3
"""Plot two pull-through candidates from one run using only the Python standard library."""

import argparse
import csv
from html import escape
from pathlib import Path


COLORS = {"normal": "#2563eb", "staging": "#e56b35"}
WIDTH, HEIGHT = 980, 560
LEFT, RIGHT, TOP, BOTTOM = 90, 40, 88, 76
PLOT_W, PLOT_H = WIDTH - LEFT - RIGHT, HEIGHT - TOP - BOTTOM


def read_candidate(directory, run_id, candidate):
    candidate_dir = directory / candidate / run_id
    stats_path = candidate_dir / "locust_stats.csv"
    history_path = candidate_dir / "locust_stats_history.csv"
    with stats_path.open(newline="") as source:
        stats = next(row for row in csv.DictReader(source) if row["Name"] == f"pull-through/{candidate}")
    with history_path.open(newline="") as source:
        history = [row for row in csv.DictReader(source) if row["Name"] == "Aggregated"]
    total = int(stats["Request Count"])
    start = int(history[0]["Timestamp"])
    active = []
    for row in history:
        active.append((int(row["Timestamp"]) - start, int(row["Total Request Count"])))
    if not active or active[-1][1] != total:
        raise ValueError(f"{candidate}: history does not contain all {total} requests")
    return {"stats": stats, "active": active}


def svg_start(title, subtitle):
    return [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH} {HEIGHT}" role="img" aria-label="{escape(title)}">',
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        f'<text x="{LEFT}" y="36" font-family="sans-serif" font-size="25" font-weight="700" fill="#172033">{escape(title)}</text>',
        f'<text x="{LEFT}" y="60" font-family="sans-serif" font-size="14" fill="#566176">{escape(subtitle)}</text>',
    ]


def axes(parts, x_max, y_max, x_label, y_label, x_ticks, y_ticks):
    def xy(x, y):
        return LEFT + PLOT_W * x / x_max, TOP + PLOT_H * (1 - y / y_max)

    for value in y_ticks:
        _, y = xy(0, value)
        parts.append(f'<line x1="{LEFT}" y1="{y:.1f}" x2="{WIDTH-RIGHT}" y2="{y:.1f}" stroke="#e5eaf0"/>')
        parts.append(f'<text x="{LEFT-12}" y="{y+5:.1f}" text-anchor="end" font-family="sans-serif" font-size="12" fill="#566176">{value:g}</text>')
    for value in x_ticks:
        x, _ = xy(value, 0)
        parts.append(f'<text x="{x:.1f}" y="{HEIGHT-BOTTOM+23}" text-anchor="middle" font-family="sans-serif" font-size="12" fill="#566176">{value:g}</text>')
    parts.append(f'<line x1="{LEFT}" y1="{TOP}" x2="{LEFT}" y2="{HEIGHT-BOTTOM}" stroke="#9aa6b5"/>')
    parts.append(f'<line x1="{LEFT}" y1="{HEIGHT-BOTTOM}" x2="{WIDTH-RIGHT}" y2="{HEIGHT-BOTTOM}" stroke="#9aa6b5"/>')
    parts.append(f'<text x="{LEFT+PLOT_W/2:.1f}" y="{HEIGHT-20}" text-anchor="middle" font-family="sans-serif" font-size="14" fill="#344054">{escape(x_label)}</text>')
    parts.append(f'<text x="25" y="{TOP+PLOT_H/2:.1f}" transform="rotate(-90 25 {TOP+PLOT_H/2:.1f})" text-anchor="middle" font-family="sans-serif" font-size="14" fill="#344054">{escape(y_label)}</text>')
    return xy


def legend(parts):
    for index, name in enumerate(("normal", "staging")):
        x = WIDTH - 240 + index * 112
        parts.append(f'<line x1="{x}" y1="56" x2="{x+24}" y2="56" stroke="{COLORS[name]}" stroke-width="4"/>')
        parts.append(f'<text x="{x+31}" y="61" font-family="sans-serif" font-size="13" fill="#344054">{name}</text>')


def line(parts, points, color, xy):
    coordinates = " ".join(f"{xy(x,y)[0]:.1f},{xy(x,y)[1]:.1f}" for x, y in points)
    parts.append(f'<polyline points="{coordinates}" fill="none" stroke="{color}" stroke-width="3" stroke-linejoin="round" stroke-linecap="round"/>')
    x, y = xy(*points[-1])
    parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="5" fill="{color}"/>')


def save(parts, output):
    output.write_text("\n".join(parts + ["</svg>"]) + "\n")


def plot_completed(candidates, output):
    parts = svg_start("Cold pull-through: completed requests", "Distinct Maven artifacts per candidate · idle and tail included")
    limit = max(points["active"][-1][0] for points in candidates.values())
    xy = axes(parts, 30 if limit <= 30 else limit, 1000, "Seconds from Locust start", "Completed requests", range(0, limit + 1, 5), range(0, 1001, 200))
    for candidate, data in candidates.items():
        line(parts, data["active"], COLORS[candidate], xy)
    legend(parts)
    save(parts, output)


def plot_throughput(candidates, output):
    series = {}
    for candidate, data in candidates.items():
        active = data["active"]
        series[candidate] = [(now[0], now[1] - previous[1]) for previous, now in zip(active, active[1:])]
    limit = max(points[-1][0] for points in series.values())
    ceiling = ((max(y for points in series.values() for _, y in points) + 9) // 10) * 10
    parts = svg_start("Cold pull-through: request throughput", "Completed requests per 1-second history interval; idle and tail included")
    xy = axes(parts, 30 if limit <= 30 else limit, ceiling, "Seconds from Locust start", "Requests per second", range(0, limit + 1, 5), range(0, ceiling + 1, 10))
    for name, points in series.items():
        line(parts, points, COLORS[name], xy)
    legend(parts)
    save(parts, output)


def plot_latency(candidates, output):
    percentiles = ["50%", "90%", "95%", "99%"]
    ceiling = 600
    parts = svg_start("Cold pull-through: response time", "Locust summary percentiles; HTTP response latency excludes the later autocommit")
    xy = axes(parts, 5, ceiling, "Latency percentile", "Response time (ms)", [], range(0, ceiling + 1, 100))
    for index, percentile in enumerate(percentiles, 1):
        x = LEFT + PLOT_W * index / 5
        parts.append(f'<text x="{x:.1f}" y="{HEIGHT-BOTTOM+23}" text-anchor="middle" font-family="sans-serif" font-size="12" fill="#566176">p{percentile[:-1]}</text>')
    for candidate, data in candidates.items():
        values = [(index, float(data["stats"][percentile])) for index, percentile in enumerate(percentiles, 1)]
        line(parts, values, COLORS[candidate], xy)
        for xval, value in values:
            x, y = xy(xval, value)
            offset = -11 if candidate == "normal" else 19
            parts.append(f'<text x="{x:.1f}" y="{y+offset:.1f}" text-anchor="middle" font-family="sans-serif" font-size="12" fill="{COLORS[candidate]}">{value:g}</text>')
    legend(parts)
    save(parts, output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=Path("."))
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output", type=Path, default=Path("plots"))
    args = parser.parse_args()
    candidates = {candidate: read_candidate(args.input, args.run_id, candidate) for candidate in ("normal", "staging")}
    args.output.mkdir(parents=True, exist_ok=True)
    plot_completed(candidates, args.output / "completed_requests.svg")
    plot_throughput(candidates, args.output / "throughput.svg")
    plot_latency(candidates, args.output / "response_time.svg")
    for candidate, data in candidates.items():
        stats = data["stats"]
        print(f"{candidate}: {stats['Request Count']} requests, {stats['Failure Count']} failures, "
              f"{stats['Requests/s']} requests/s, {float(stats['Average Response Time']):.1f} ms mean")


if __name__ == "__main__":
    main()
