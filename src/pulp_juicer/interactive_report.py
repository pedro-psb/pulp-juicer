"""Render side-by-side, cursor-synchronized uPlot timelines."""

import html
import json
import math
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from .report import completed_runs
from .report_navigation import navigation_html
from .result_data import normalize_execution, read_rows
from .tasking import TASK_STATES
from .trial_report import _METRICS, _read_trial

_LAYOUT_MARKER = "<!-- pulp-juicer-interactive-layout: 20 -->"
_ASSETS = Path(__file__).with_name("assets")
_METRIC_GROUPS = (
    ("Request, Tasks and Postgres activity", ("requests", "tasks", "postgres")),
    ("Content-worker storage", ("content_storage",)),
    (
        "API-worker and temporary-file activity",
        ("requests", "api_resources", "api_storage", "temporary_files"),
    ),
)


def _series_for_browser(series):
    """Keep uPlot's x values sorted and valid, even with noisy collector samples."""
    result = {}
    for metric, points in series.items():
        valid = {}
        for x, y in points:
            if math.isfinite(x) and math.isfinite(y):
                valid[x] = y
        if valid:
            result[metric] = sorted(valid.items())
    return result


def _request_markers(run_dir, metadata):
    rows = read_rows(run_dir, "requests")
    starts = [row["started_at_utc"] for row in rows if row["started_at_utc"]]
    completions = [row["completed_at_utc"] for row in rows if row["completed_at_utc"]]
    if not starts or not completions:
        return None
    saved_start = (metadata or {}).get("result", {}).get("load_start_utc")
    origin = datetime.fromisoformat(saved_start) if saved_start else min(starts)
    return {
        "start": (min(starts) - origin).total_seconds(),
        "end": (max(completions) - origin).total_seconds(),
        "start_abs": min(starts).timestamp(),
        "end_abs": max(completions).timestamp(),
    }


def render_interactive_report(execution_dir: Path) -> Path | None:
    """Write interactive.html for all complete runs in an execution."""
    execution_dir = Path(execution_dir)
    run_dirs = list(completed_runs(execution_dir))
    if not run_dirs:
        return None
    normalize_execution(execution_dir)
    output = execution_dir / "interactive.html"
    if output.is_file() and _LAYOUT_MARKER in output.read_text():
        data_inputs = (
            path
            for pattern in (
                "*/*/*/*.parquet",
                "*/*/*/collectors/*.parquet",
                "*/*/*/run_metadata.json",
            )
            for path in execution_dir.glob(pattern)
        )
        inputs = (
            Path(__file__),
            Path(__file__).with_name("report_navigation.py"),
            *_ASSETS.iterdir(),
            *data_inputs,
        )
        for extra in (
            execution_dir / "execution.log",
            execution_dir / "timings.parquet",
        ):
            if extra.is_file():
                inputs = (*inputs, extra)
        if all(path.stat().st_mtime_ns <= output.stat().st_mtime_ns for path in inputs):
            return output.resolve()

    scenarios = defaultdict(lambda: defaultdict(list))
    for run_dir in run_dirs:
        trial = _read_trial(run_dir)
        if trial is None:
            continue
        markers = _request_markers(run_dir, trial.metadata)
        scenarios[trial.scenario][trial.candidate].append(
            {
                "run": trial.run_id,
                "series": _series_for_browser(trial.series),
                "markers": {key: markers[key] for key in ("start", "end")}
                if markers
                else None,
            }
        )
    if not scenarios:
        return None

    metrics = {
        key: {"key": key, "title": title, "axis": axis} for key, title, axis in _METRICS
    }
    metrics["requests"] = {
        "key": "requests",
        "title": "Request activity",
        "axis": "New requests/s",
        "primary": "starts",
        "secondary": "throughput",
        "secondaryAxis": "Completed requests/s",
        "secondaryScale": "y",
        "subtitle": "New requests/s · completed requests/s · cumulative completed",
    }
    metrics["tasks"] = {
        "key": "tasks",
        "title": "Task activity",
        "axis": "Tasks (count)",
        "traces": [
            {
                "key": f"task_{key}",
                "label": label,
                "color": color,
                "defaultVisible": key != "waiting_unblocked",
            }
            for key, label, color in TASK_STATES
        ],
        "subtitle": "Task counts by state; select Waiting (unblocked) in the legend to show it",
    }
    metrics["postgres"] = {
        "key": "postgres",
        "title": "PostgreSQL CPU and memory",
        "axis": metrics["cpu"]["axis"],
        "primary": "cpu",
        "secondary": "memory",
        "secondaryAxis": metrics["memory"]["axis"],
        "secondaryAxisLabel": "PSS (MiB)",
        "secondaryScale": "memory",
        "subtitle": "CPU (% of one core) · PSS (MiB)",
    }
    metrics["api_resources"] = {
        "key": "api_resources",
        "title": "API-worker CPU and memory",
        "axis": metrics["api_cpu"]["axis"],
        "primary": "api_cpu",
        "secondary": "api_memory",
        "secondaryAxis": metrics["api_memory"]["axis"],
        "secondaryAxisLabel": "PSS (MiB)",
        "secondaryScale": "api_memory",
        "subtitle": "CPU (% of one core) · PSS (MiB)",
    }
    metrics["api_storage"] = {
        "key": "api_storage",
        "title": "API-worker storage I/O",
        "axis": metrics["api_read"]["axis"],
        "primary": "api_read",
        "secondary": "api_write",
        "secondaryAxis": metrics["api_write"]["axis"],
        "secondaryScale": "y",
        "subtitle": "Read and write (MiB/s)",
    }
    metrics["content_storage"] = {
        "key": "content_storage",
        "title": "Content-worker storage I/O",
        "axis": metrics["content_read"]["axis"],
        "primary": "content_read",
        "secondary": "content_write",
        "secondaryAxis": metrics["content_write"]["axis"],
        "secondaryScale": "y",
        "subtitle": "Read and write (MiB/s)",
    }
    metrics["temporary_files"] = {
        "key": "temporary_files",
        "title": "Pulp temporary files",
        "axis": metrics["temp_files"]["axis"],
        "primary": "temp_files",
        "secondary": "temp_bytes",
        "secondaryAxis": metrics["temp_bytes"]["axis"],
        "secondaryAxisLabel": "Size (MiB)",
        "secondaryScale": "temp_size",
        "subtitle": "Active file count · apparent size (MiB)",
    }
    data = {
        "scenarios": [
            {
                "name": scenario,
                "candidates": [
                    {
                        "name": candidate,
                        "runs": sorted(runs, key=lambda run: run["run"]),
                    }
                    for candidate, runs in sorted(candidates.items())
                ],
            }
            for scenario, candidates in sorted(scenarios.items())
        ],
        "groups": [
            {"title": title, "metrics": [metrics[key] for key in keys]}
            for title, keys in _METRIC_GROUPS
        ],
    }
    # JSON is embedded in a script tag; escape HTML-sensitive characters in labels.
    payload = (
        json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )
    title = html.escape(execution_dir.name)
    page = (
        _PAGE.replace("__TITLE__", title)
        .replace("__NAV__", navigation_html("interactive.html"))
        .replace("__DATA__", payload)
        .replace("__UPLOT_CSS__", (_ASSETS / "uPlot.min.css").read_text())
        .replace("__UPLOT_JS__", (_ASSETS / "uPlot.iife.min.js").read_text())
        .replace("__UPLOT_LICENSE__", (_ASSETS / "LICENSE.uPlot").read_text())
    )
    output.write_text(page, encoding="utf-8")
    return output.resolve()


_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Interactive timelines: __TITLE__</title>
<style>__UPLOT_CSS__</style>
<style>
  :root { font-family: system-ui, sans-serif; color: #17233a; background: #f5f7fb; }
  body { margin: 0; }
  main { max-width: 1800px; margin: auto; padding: 24px; }
  h1 { margin: 0 0 8px; }
  .intro { color: #526178; margin-bottom: 24px; }
  .marker-legend { color: #526178; margin: -12px 0 24px; }
  .marker-legend span { display: inline-flex; align-items: center; gap: 6px; margin-right: 18px; }
  .marker-legend i { display: inline-block; width: 18px; border-top: 1px dashed; }
  .marker-legend .start i { border-color: #8cae9e; }
  .marker-legend .end i { border-color: #c7ad90; }
  .marker-legend .window i { height: 10px; background: #f0f4f7; border: 1px solid #e2eaf0; }
  a { color: #2256b2; }
  section { margin: 28px 0 44px; }
  .scroll { overflow-x: auto; padding-bottom: 8px; }
  .matrix { display: grid; gap: 10px; width: max-content; min-width: 100%; }
  .matrix > * { box-sizing: border-box; }
  .corner, .candidate, .metric-label, .chart-cell { background: white; border: 1px solid #dde4ef; border-radius: 8px; }
  .corner, .metric-label { padding: 14px; }
  .candidate { padding: 12px 14px; position: sticky; top: 0; z-index: 2; }
  .candidate strong { display: block; margin-bottom: 8px; }
  .candidate select { width: 100%; padding: 6px; }
  .shortcut-hint { display: block; margin-top: 5px; color: #627189; font-size: .78rem; }
  .group-heading { grid-column: 1 / -1; margin: 14px 0 0; padding: 12px 14px; background: #e8eef7; border-left: 4px solid #8ba4c7; border-radius: 6px; font-size: 1rem; }
  .metric-label strong { display: block; }
  .metric-label small { color: #526178; }
  .chart-cell { padding: 8px 4px; min-height: 240px; }
  .empty { display: grid; place-items: center; height: 210px; color: #69778d; }
  .uplot { margin: auto; }
  .u-legend { font-size: 12px; }
</style>
</head>
<body>
__NAV__
<main>
<h1>Interactive timelines</h1>
<p class="intro">Execution: <strong>__TITLE__</strong> · Time is seconds since each run's load start. Choose one run per candidate; hover a chart to see the same time in every chart in that scenario.</p>
<p class="intro">Press a/s/d/f for the next run in candidate columns 1–4, or Shift+letter for the previous run. Shortcuts cycle each scenario.</p>
<p class="intro">Blue shows the primary series and orange the second measurement on combined charts. Only Request activity has a dashed gray cumulative completed-requests line on a right axis.</p>
<p class="marker-legend"><span class="window"><i></i>Input window</span><span class="start"><i></i>Input starts (first request)</span><span class="end"><i></i>Input ends (all requests completed)</span></p>
<div id="reports"></div>
</main>
<script id="report-data" type="application/json">__DATA__</script>
<!-- uPlot 1.6.32 license:
__UPLOT_LICENSE__
-->
<script>__UPLOT_JS__</script>
<script>
(() => {
  const data = JSON.parse(document.getElementById("report-data").textContent);
  const root = document.getElementById("reports");
  if (typeof uPlot === "undefined") {
    root.textContent = "uPlot could not load in this browser.";
    return;
  }
  const chartGroups = [];
  const shortcutKeys = ["a", "s", "d", "f"];
  const scenarioSelectors = [];
  const make = (tag, className, text) => {
    const element = document.createElement(tag);
    element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
  };
  const drawInputWindow = markers => plot => {
    if (!markers || markers.end <= markers.start) return;
    const {ctx, bbox} = plot;
    const start = plot.valToPos(markers.start, "x", true);
    const end = plot.valToPos(markers.end, "x", true);
    const left = Math.max(bbox.left, start);
    const right = Math.min(bbox.left + bbox.width, end);
    if (right <= left) return;
    ctx.save();
    ctx.fillStyle = "rgba(102, 129, 157, 0.08)";
    ctx.fillRect(left, bbox.top, right - left, bbox.height);
    ctx.restore();
  };
  const drawMarkers = markers => plot => {
    if (!markers) return;
    const {ctx, bbox, pxRatio} = plot;
    ctx.save();
    ctx.lineWidth = pxRatio;
    ctx.globalAlpha = 0.75;
    ctx.setLineDash([3 * pxRatio, 5 * pxRatio]);
    for (const [name, color] of [["start", "#8cae9e"], ["end", "#c7ad90"]]) {
      const value = markers[name];
      if (value == null) continue;
      const x = plot.valToPos(value, "x", true);
      if (x < bbox.left || x > bbox.left + bbox.width) continue;
      ctx.beginPath();
      ctx.strokeStyle = color;
      ctx.moveTo(x, bbox.top);
      ctx.lineTo(x, bbox.top + bbox.height);
      ctx.stroke();
    }
    ctx.restore();
  };
  data.scenarios.forEach((scenario, scenarioIndex) => {
    const section = make("section", "");
    section.append(make("h2", "", scenario.name));
    const scroll = make("div", "scroll");
    const matrix = make("div", "matrix");
    matrix.style.gridTemplateColumns = `180px repeat(${scenario.candidates.length}, minmax(460px, 1fr))`;
    scroll.append(matrix);
    section.append(scroll);
    root.append(section);
    matrix.append(make("div", "corner", "Measurement"));
    const selected = scenario.candidates.map(() => 0);
    const selectors = [];
    const chartRows = [];
    const plots = [];
    for (const [index, candidate] of scenario.candidates.entries()) {
      const header = make("div", "candidate");
      header.append(make("strong", "", candidate.name));
      const selector = make("select", "");
      selector.setAttribute("aria-label", `${scenario.name}: run for ${candidate.name}`);
      candidate.runs.forEach((run, runIndex) => {
        const option = make("option", "", run.run);
        option.value = String(runIndex);
        selector.append(option);
      });
      selector.addEventListener("change", () => {
        selected[index] = Number(selector.value);
        draw();
      });
      header.append(selector);
      if (index < shortcutKeys.length) {
        const key = shortcutKeys[index];
        header.append(make("small", "shortcut-hint", `${key} next · ${key.toUpperCase()} previous`));
      }
      selectors.push(selector);
      matrix.append(header);
    }
    for (const group of data.groups) {
      matrix.append(make("h3", "group-heading", group.title));
      for (const metric of group.metrics) {
        const label = make("div", "metric-label");
        label.append(make("strong", "", metric.title), make("small", "", metric.subtitle || metric.axis));
        matrix.append(label);
        const cells = [];
        for (const candidate of scenario.candidates) {
          const cell = make("div", "chart-cell");
          cell.setAttribute("aria-label", `${candidate.name}: ${metric.title}`);
          matrix.append(cell);
          cells.push(cell);
        }
        chartRows.push({metric, cells});
      }
    }
    function draw() {
      plots.forEach(plot => plot.destroy());
      plots.length = 0;
      chartRows.flatMap(row => row.cells).forEach(cell => cell.replaceChildren());
      const runs = scenario.candidates.map((candidate, index) => candidate.runs[selected[index]]);
      let minX = 0;
      let maxX = 1;
      for (const run of runs) for (const points of Object.values(run.series)) {
        for (const [x] of points) {
          minX = Math.min(minX, x);
          maxX = Math.max(maxX, x);
        }
      }
      for (const run of runs) if (run.markers) {
        minX = Math.min(minX, run.markers.start);
        maxX = Math.max(maxX, run.markers.end);
      }
      const padding = Math.max(0.1, (maxX - minX) * 0.02);
      minX -= padding;
      maxX += padding;
      for (const {metric, cells} of chartRows) {
        if (metric.traces) {
          const traceRows = runs.map(run => metric.traces.map(trace => run.series[trace.key] || []));
          let maxY = 1;
          for (const candidate of traceRows) for (const points of candidate) {
            for (const [, y] of points) maxY = Math.max(maxY, y);
          }
          maxY *= 1.05;
          traceRows.forEach((candidate, candidateIndex) => {
            const cell = cells[candidateIndex];
            if (!candidate.some(points => points.length)) {
              cell.append(make("div", "empty", "No samples for this run"));
              return;
            }
            const tables = candidate.map(points => [points.map(point => point[0]), points.map(point => point[1])]);
            const aligned = uPlot.join(tables);
            const series = [
              {label: "Seconds"},
              ...metric.traces.map(trace => ({
                label: trace.label, stroke: trace.color, width: 2,
                show: trace.defaultVisible !== false,
                spanGaps: true, points: {show: false},
              })),
            ];
            plots.push(new uPlot({
              width: Math.max(420, cell.clientWidth - 12), height: 280,
              scales: {x: {time: false, range: [minX, maxX]}, y: {range: [0, maxY]}},
              series, axes: [{label: "Seconds"}, {size: 52, label: metric.axis}],
              cursor: {
                drag: {x: false, y: false},
                sync: {key: `scenario-${scenarioIndex}`, scales: ["x", null], setSeries: false},
              },
              hooks: {
                drawClear: [drawInputWindow(runs[candidateIndex].markers)],
                draw: [drawMarkers(runs[candidateIndex].markers)],
              },
            }, aligned, cell));
          });
          continue;
        }
        let maxCompleted = 1;
        if (metric.key === "requests") {
          for (const run of runs) for (const [, count] of run.series.completed || []) {
            maxCompleted = Math.max(maxCompleted, count);
          }
          maxCompleted *= 1.05;
        }
        const rows = runs.map(run => run.series[metric.primary || metric.key] || []);
        const secondaryRows = metric.secondary ? runs.map(run => run.series[metric.secondary] || []) : [];
        let maxY = 1;
        for (const points of rows) for (const [, y] of points) maxY = Math.max(maxY, y);
        if (metric.secondaryScale === "y") {
          for (const points of secondaryRows) for (const [, y] of points) maxY = Math.max(maxY, y);
        }
        maxY *= 1.05;
        let maxSecondary = 1;
        for (const points of secondaryRows) for (const [, y] of points) maxSecondary = Math.max(maxSecondary, y);
        maxSecondary *= 1.05;
        rows.forEach((points, candidateIndex) => {
          const cell = cells[candidateIndex];
          const secondary = secondaryRows[candidateIndex] || [];
          if (!points.length && !secondary.length) {
            cell.append(make("div", "empty", "No samples for this run"));
            return;
          }
          const tables = [[points.map(point => point[0]), points.map(point => point[1])]];
          if (secondary.length) tables.push([secondary.map(point => point[0]), secondary.map(point => point[1])]);
          const completed = metric.key === "requests" ? runs[candidateIndex].series.completed || [] : [];
          const hasOverlay = completed.length > 0;
          if (hasOverlay) tables.push([completed.map(point => point[0]), completed.map(point => point[1])]);
          const aligned = tables.length > 1 ? uPlot.join(tables) : tables[0];
          const scales = {x: {time: false, range: [minX, maxX]}, y: {range: [0, maxY]}};
          const series = [
            {label: "Seconds"},
            {label: metric.axis, stroke: "#2563eb", width: 2, spanGaps: true, points: {show: false}},
          ];
          const axes = [{label: "Seconds"}, {size: 52}];
          if (secondary.length) {
            if (metric.secondaryScale !== "y") scales[metric.secondaryScale] = {range: [0, maxSecondary]};
            series.push({
              label: metric.secondaryAxis, scale: metric.secondaryScale, stroke: "#e56b35",
              width: 2, spanGaps: true, points: {show: false},
            });
            if (metric.secondaryScale !== "y") {
              axes.push({scale: metric.secondaryScale, side: 1, label: metric.secondaryAxisLabel, size: 58, grid: {show: false}});
            }
          }
          if (hasOverlay) {
            scales.requests = {range: [0, maxCompleted]};
            series.push({
              label: "Completed requests", scale: "requests", stroke: "#64748b",
              width: 1.5, dash: [5, 4], spanGaps: true, points: {show: false},
            });
            axes.push({scale: "requests", side: 1, label: "Completed requests", size: 58, grid: {show: false}});
          }
          plots.push(new uPlot({
            width: Math.max(420, cell.clientWidth - 12), height: 220,
            scales, series, axes,
            cursor: {
              drag: {x: false, y: false},
              sync: {key: `scenario-${scenarioIndex}`, scales: ["x", null], setSeries: false},
            },
            hooks: {
              drawClear: [drawInputWindow(runs[candidateIndex].markers)],
              draw: [drawMarkers(runs[candidateIndex].markers)],
            },
          }, aligned, cell));
        });
      }
    }
    draw();
    scenarioSelectors.push(selectors);
    chartGroups.push({plots});
  });
  window.addEventListener("keydown", event => {
    if (event.altKey || event.ctrlKey || event.metaKey || event.repeat) return;
    const target = event.target;
    if (target instanceof HTMLElement && (target.isContentEditable || ["INPUT", "TEXTAREA"].includes(target.tagName))) return;
    const index = shortcutKeys.indexOf(event.key.toLowerCase());
    if (index < 0) return;
    const direction = event.key === event.key.toUpperCase() ? -1 : 1;
    let changed = false;
    for (const selectors of scenarioSelectors) {
      const selector = selectors[index];
      if (!selector || selector.options.length < 2) continue;
      selector.selectedIndex = (selector.selectedIndex + direction + selector.options.length) % selector.options.length;
      selector.dispatchEvent(new Event("change"));
      changed = true;
    }
    if (changed) event.preventDefault();
  }, true);
  let resizeTimer;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => chartGroups.forEach(({plots}) => {
      plots.forEach(plot => plot.setSize({width: Math.max(420, plot.root.parentElement.clientWidth - 12), height: 220}));
    }), 100);
  });
})();
</script>
__LAYOUT_MARKER__
</body>
</html>
""".replace("__LAYOUT_MARKER__", _LAYOUT_MARKER)
