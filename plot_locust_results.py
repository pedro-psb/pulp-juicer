#!/usr/bin/env python3
"""Plot two Locust pull-through runs using only the Python standard library."""

import argparse
import csv
from html import escape
from pathlib import Path


COLORS = {"normal": "#2563eb", "staging": "#e56b35"}
WIDTH, HEIGHT = 980, 560
LEFT, RIGHT, TOP, BOTTOM = 90, 40, 88, 76
PLOT_W, PLOT_H = WIDTH - LEFT - RIGHT, HEIGHT - TOP - BOTTOM


def read_run(directory, name):
    with (directory / f"{name}_stats.csv").open(newline="") as source:
        stats = next(row for row in csv.DictReader(source) if row["Name"] == f"pull-through/{name}")
    with (directory / f"{name}_stats_history.csv").open(newline="") as source:
        history = [row for row in csv.DictReader(source) if row["Name"] == "Aggregated"]
    total = int(stats["Request Count"])
    start = int(history[0]["Timestamp"])
    active = []
    for row in history:
        active.append((int(row["Timestamp"]) - start, int(row["Total Request Count"])))
        if active[-1][1] >= total:
            break
    if not active or active[-1][1] != total:
        raise ValueError(f"{name}: history does not contain all {total} requests")
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


def plot_completed(runs, output):
    parts = svg_start("Cold pull-through: completed requests", "1,000 distinct Maven artifacts per mode · 10 users · zero think time")
    limit = max(points["active"][-1][0] for points in runs.values())
    xy = axes(parts, 30 if limit <= 30 else limit, 1000, "Seconds from Locust start", "Completed requests", range(0, limit + 1, 5), range(0, 1001, 200))
    for name, data in runs.items():
        line(parts, data["active"], COLORS[name], xy)
    legend(parts)
    save(parts, output)


def plot_throughput(runs, output):
    series = {}
    for name, data in runs.items():
        active = data["active"]
        series[name] = [(now[0], now[1] - previous[1]) for previous, now in zip(active, active[1:])]
    limit = max(points[-1][0] for points in series.values())
    ceiling = ((max(y for points in series.values() for _, y in points) + 9) // 10) * 10
    parts = svg_start("Cold pull-through: request throughput", "Completed requests per 1-second history interval; active request window only")
    xy = axes(parts, 30 if limit <= 30 else limit, ceiling, "Seconds from Locust start", "Requests per second", range(0, limit + 1, 5), range(0, ceiling + 1, 10))
    for name, points in series.items():
        line(parts, points, COLORS[name], xy)
    legend(parts)
    save(parts, output)


def plot_latency(runs, output):
    percentiles = ["50%", "90%", "95%", "99%"]
    ceiling = 600
    parts = svg_start("Cold pull-through: response time", "Locust summary percentiles; HTTP response latency excludes the later autocommit")
    xy = axes(parts, 5, ceiling, "Latency percentile", "Response time (ms)", [], range(0, ceiling + 1, 100))
    for index, percentile in enumerate(percentiles, 1):
        x = LEFT + PLOT_W * index / 5
        parts.append(f'<text x="{x:.1f}" y="{HEIGHT-BOTTOM+23}" text-anchor="middle" font-family="sans-serif" font-size="12" fill="#566176">p{percentile[:-1]}</text>')
    for name, data in runs.items():
        values = [(index, float(data["stats"][percentile])) for index, percentile in enumerate(percentiles, 1)]
        line(parts, values, COLORS[name], xy)
        for xval, value in values:
            x, y = xy(xval, value)
            offset = -11 if name == "normal" else 19
            parts.append(f'<text x="{x:.1f}" y="{y+offset:.1f}" text-anchor="middle" font-family="sans-serif" font-size="12" fill="{COLORS[name]}">{value:g}</text>')
    legend(parts)
    save(parts, output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=Path("."))
    parser.add_argument("--output", type=Path, default=Path("plots"))
    args = parser.parse_args()
    runs = {name: read_run(args.input, name) for name in ("normal", "staging")}
    args.output.mkdir(parents=True, exist_ok=True)
    plot_completed(runs, args.output / "completed_requests.svg")
    plot_throughput(runs, args.output / "throughput.svg")
    plot_latency(runs, args.output / "response_time.svg")
    for name, data in runs.items():
        stats = data["stats"]
        print(f"{name}: {stats['Request Count']} requests, {stats['Failure Count']} failures, "
              f"{stats['Requests/s']} requests/s, {float(stats['Average Response Time']):.1f} ms mean")


if __name__ == "__main__":
    main()
