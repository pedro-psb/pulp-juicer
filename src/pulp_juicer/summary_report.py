"""Render the selected-run and across-run statistics as a standalone page."""

import html
import json
from collections import defaultdict
from pathlib import Path

from .catalog import experiments
from .execution_timing import read_execution_timings
from .interactive_report import _request_markers
from .report import completed_runs
from .report_navigation import navigation_html
from .result_data import normalize_execution
from .summary_statistics import (
    VARIABLES,
    summarize_candidate,
    summarize_candidate_task_types,
    summarize_run,
    summarize_task_types,
)
from .trial_report import _read_trial

_LAYOUT_MARKER = "<!-- pulp-juicer-summary-layout: 16 -->"


def render_summary_report(execution_dir: Path) -> Path | None:
    """Write summary.html for all complete runs in an execution."""
    execution_dir = Path(execution_dir)
    run_dirs = list(completed_runs(execution_dir))
    if not run_dirs:
        return None
    normalize_execution(execution_dir)
    registry = experiments()
    output = execution_dir / "summary.html"
    if output.is_file() and _LAYOUT_MARKER in output.read_text():
        inputs = (
            Path(__file__),
            Path(__file__).with_name("interactive_report.py"),
            Path(__file__).with_name("summary_statistics.py"),
            Path(__file__).with_name("report_navigation.py"),
            *(compose.parent / "experiment.py" for _, compose in registry.values()),
            *(
                path
                for pattern in (
                    "*/*/*/*.parquet",
                    "*/*/*/collectors/*.parquet",
                    "*/*/*/run_metadata.json",
                    "timings.parquet",
                )
                for path in execution_dir.glob(pattern)
            ),
        )
        if all(path.stat().st_mtime_ns <= output.stat().st_mtime_ns for path in inputs):
            return output.resolve()

    timings = read_execution_timings(execution_dir)
    scenarios = defaultdict(lambda: defaultdict(list))
    recorded_experiment_id = None
    for run_dir in run_dirs:
        trial = _read_trial(run_dir)
        if trial is None:
            continue
        recorded_experiment_id = (trial.metadata or {}).get(
            "experiment"
        ) or recorded_experiment_id
        markers = _request_markers(run_dir, trial.metadata)
        timing = timings.get((trial.scenario, trial.candidate, trial.run_id))
        scenarios[trial.scenario][trial.candidate].append(
            {
                "run": trial.run_id,
                "statistics": summarize_run(trial, markers, timing),
                "task_types": summarize_task_types(trial.tasks),
            }
        )
    if not scenarios:
        return None
    experiment_id = recorded_experiment_id or execution_dir.name.split("__", 1)[0]
    selected_experiment = registry.get(experiment_id)
    scenario_configs = (
        {scenario.id: scenario for scenario in selected_experiment[0].scenarios}
        if selected_experiment
        else {}
    )
    variable_keys = {key for _, key, _, _, _, _ in VARIABLES}
    for scenario_id in scenarios:
        scenario = scenario_configs.get(scenario_id)
        if scenario is None:
            continue
        unknown = set(scenario.selected_stats) - variable_keys - {"trial_phases"}
        if unknown:
            raise ValueError(
                f"Unknown selected_stats variables in {scenario.id}: {', '.join(sorted(unknown))}"
            )
    data = {
        "scenarios": [
            {
                "name": scenario,
                "selected_stats": list(scenario_configs[scenario].selected_stats)
                if scenario in scenario_configs
                else [],
                "candidates": [
                    {
                        "name": candidate,
                        "runs": sorted(runs, key=lambda run: run["run"]),
                        "statistics": summarize_candidate(runs),
                        "task_types": summarize_candidate_task_types(runs),
                    }
                    for candidate, runs in sorted(candidates.items())
                ],
            }
            for scenario, candidates in sorted(scenarios.items())
        ],
        "variables": [
            {
                "group": group,
                "key": key,
                "title": title,
                "unit": unit,
                "measure": measure,
                "fields": fields,
            }
            for group, key, title, unit, measure, fields in VARIABLES
        ],
    }
    payload = (
        json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )
    page = (
        _PAGE.replace("__TITLE__", html.escape(execution_dir.name))
        .replace("__NAV__", navigation_html("summary.html"))
        .replace("__DATA__", payload)
    )
    output.write_text(page, encoding="utf-8")
    return output.resolve()


_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Summary statistics: __TITLE__</title>
<style>
  :root { font-family: system-ui, sans-serif; color: #17233a; background: #f5f7fb; }
  body { margin: 0; }
  main { max-width: 1800px; margin: auto; padding: 24px; }
  h1 { margin: 0 0 8px; }
  .intro, .summary-help { color: #526178; }
  section { margin: 28px 0 44px; }
  .controls { display: flex; flex-wrap: wrap; gap: 14px; align-items: center; margin: 12px 0; }
  .controls label { display: flex; align-items: center; gap: 8px; }
  .controls select { padding: 5px; }
  .run-controls { display: flex; flex-wrap: wrap; gap: 12px; margin: 12px 0; }
  .run-controls label { display: flex; align-items: center; gap: 8px; }
  .run-controls select { padding: 5px; }
  .shortcut-hint { color: #627189; font-size: .78rem; }
  .summary-help { font-size: .88rem; margin: 8px 0 14px; }
  .summary-group { margin: 14px 0 24px; }
  .group-heading { margin: 14px 0 0; padding: 12px 14px; background: #e8eef7; border-left: 4px solid #8ba4c7; border-radius: 6px; font-size: 1rem; }
  .subgroup-title { margin: 16px 14px 6px; font-size: .95rem; }
  .summary-table-wrap { overflow-x: auto; margin: 10px 14px 18px; }
  .summary-table { border-collapse: collapse; width: 100%; min-width: max-content; font-size: .82rem; }
  .summary-table th, .summary-table td { padding: 8px 10px; border-top: 1px solid #e4e9f1; text-align: left; vertical-align: top; }
  .summary-table thead th, .summary-table tbody th { background: #f7f9fc; }
  .summary-table .variable-label, .summary-table thead tr:first-child th:first-child { min-width: 190px; position: sticky; left: 0; z-index: 1; background: #f7f9fc; }
  .summary-table .variable-label strong, .summary-table .variable-label small { display: block; }
  .summary-table .variable-label small { color: #627189; font-weight: 400; margin-top: 3px; }
  .summary-table .candidate-label { min-width: 125px; }
  .summary-table th:not(.variable-label), .summary-table td { min-width: 125px; }
  .summary-table tbody th { font-weight: 600; }
  .summary-table tbody tr.variable-band-alt > * { background: #eaf1fb; }
</style>
</head>
<body>
__NAV__
<main>
<h1>Summary statistics</h1>
<p class="intro">Execution: <strong>__TITLE__</strong></p>
<p class="intro">Press a/s/d/f for the next run in candidates 1–4, or Shift+letter for the previous run. Shortcuts cycle each scenario in the selected-run view.</p>
<p class="summary-help">Within a run: request and task events use population SD and interpolated percentiles. Collector values are weighted by observed time in the input window; their percentiles describe time occupancy. Task events include drain time. Across runs: each run contributes equally; SD is sample SD (N/A for one run). Missing measurements are excluded. Rate totals are integrated over observed intervals.</p>
<div id="reports"></div>
</main>
<script id="report-data" type="application/json">__DATA__</script>
<script>
(() => {
  const data = JSON.parse(document.getElementById("report-data").textContent);
  const root = document.getElementById("reports");
  const shortcutKeys = ["a", "s", "d", "f"];
  const scenarioControls = [];
  const make = (tag, className, text) => {
    const element = document.createElement(tag);
    element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
  };
  const number = value => value == null ? "N/A" : Number(value).toLocaleString(undefined, {maximumFractionDigits: 2});
  const fieldName = {n: "n", mean: "mean", sd: "SD", median: "median", p95: "p95", p99: "p99", max: "max", total: "total", percent: "failure %"};
  const renderValue = (cell, value, mode, percent = null, suffix = "") => {
    if (value == null) {
      cell.textContent = "N/A";
    } else if (mode === "run") {
      cell.textContent = `${number(value)}${suffix}${percent == null ? "" : ` (${number(percent)}%)`}`;
    } else {
      cell.textContent = `${number(value.mean)}${suffix} ± ${number(value.sd)}${suffix}`;
    }
  };
  const summaryFor = (candidate, index, key, selected, mode) =>
    mode === "run" ? candidate.runs[selected[index]].statistics[key] : candidate.statistics[key];
  const table = (head, body) => {
    const wrap = make("div", "summary-table-wrap");
    const element = make("table", "summary-table");
    element.append(head, body);
    wrap.append(element);
    return wrap;
  };
  const variableLabel = (variable, mode) => {
    const label = make("th", "variable-label");
    label.append(make("strong", "", variable.title), make("small", "", `${variable.unit} · ${variable.measure}`));
    if (variable.fields.includes("n")) {
      const counted = {
        request: "measured requests",
        "one-second bin": "one-second intervals",
        "Locust history sample": "Locust history samples",
        task: "measured tasks",
        "collector interval": "collector intervals",
      }[variable.unit] || `${variable.unit} observations`;
      label.append(make("small", "n-meaning", `n = ${counted}${mode === "candidate" ? " per run" : ""}`));
    }
    return label;
  };
  const batchesFor = variables => {
    const batches = new Map();
    for (const variable of variables) {
      const key = variable.fields.filter(field => field !== "total").join("|");
      if (!batches.has(key)) batches.set(key, {fields: [...variable.fields], variables: []});
      const batch = batches.get(key);
      for (const field of variable.fields) if (!batch.fields.includes(field)) batch.fields.push(field);
      batch.variables.push(variable);
    }
    return [...batches.values()];
  };
  const selectedBatchesFor = variables => {
    const batches = [];
    for (const variable of variables) {
      const key = variable.fields.filter(field => field !== "total").join("|");
      const previous = batches.at(-1);
      if (previous?.key !== key) batches.push({key, fields: [...variable.fields], variables: []});
      const batch = batches.at(-1);
      for (const field of variable.fields) if (!batch.fields.includes(field)) batch.fields.push(field);
      batch.variables.push(variable);
    }
    return batches;
  };
  const renderGenericBatch = (groupHost, batch, scenario, selected, mode) => {
    const head = make("thead", "");
    const labels = make("tr", "");
    labels.append(make("th", "", "Variable"), make("th", "", "Candidate"));
    batch.fields.forEach(field => labels.append(make("th", "", fieldName[field] || field)));
    head.append(labels);
    const body = make("tbody", "");
    for (const [variableIndex, variable] of batch.variables.entries()) {
      scenario.candidates.forEach((candidate, index) => {
        const row = make("tr", variableIndex % 2 ? "variable-band-alt" : "");
        if (index === 0) {
          const label = variableLabel(variable, mode);
          label.rowSpan = scenario.candidates.length;
          label.setAttribute("scope", "rowgroup");
          row.append(label);
        }
        const name = make("th", "candidate-label", candidate.name);
        name.setAttribute("scope", "row");
        row.append(name);
        const summary = summaryFor(candidate, index, variable.key, selected, mode);
        batch.fields.forEach(field => {
          const cell = make("td", "");
          renderValue(cell, summary?.[field], mode, null, field === "percent" ? "%" : "");
          row.append(cell);
        });
        body.append(row);
      });
    }
    groupHost.append(table(head, body));
  };
  const renderSelectedPhases = (groupHost, scenario, selected, mode) => {
    const phases = data.variables.filter(variable => variable.group === "Trial phases");
    if (!phases.length) return;
    groupHost.append(make("h3", "subgroup-title", "Trial phases"));
    const head = make("thead", "");
    const labels = make("tr", "");
    labels.append(make("th", "", "Candidate"));
    phases.forEach(phase => labels.append(make("th", "", `${phase.title} (s)`)));
    head.append(labels);
    const body = make("tbody", "");
    scenario.candidates.forEach((candidate, index) => {
      const row = make("tr", "");
      const name = make("th", "candidate-label", candidate.name);
      name.setAttribute("scope", "row");
      row.append(name);
      phases.forEach(phase => {
        const cell = make("td", "");
        renderValue(cell, summaryFor(candidate, index, phase.key, selected, mode)?.total, mode);
        row.append(cell);
      });
      body.append(row);
    });
    groupHost.append(table(head, body));
  };
  const renderTaskOutcomes = (groupHost, scenario, selected, mode) => {
    groupHost.append(make("h3", "subgroup-title", "Task outcomes"));
    const summaries = scenario.candidates.map((candidate, index) => summaryFor(candidate, index, "task_status", selected, mode));
    const statuses = [...new Set(summaries.flatMap(summary => Object.keys(summary?.counts || {})))].sort();
    const head = make("thead", "");
    const labels = make("tr", "");
    labels.append(make("th", "", "Variable"), make("th", "", "Candidate"), make("th", "", "Count"), make("th", "", "%"));
    head.append(labels);
    const body = make("tbody", "");
    for (const [statusIndex, status] of (statuses.length ? statuses : ["No observations"]).entries()) {
      summaries.forEach((summary, index) => {
        const row = make("tr", statusIndex % 2 ? "variable-band-alt" : "");
        if (index === 0) {
          const label = make("th", "variable-label", status);
          label.rowSpan = scenario.candidates.length;
          label.setAttribute("scope", "rowgroup");
          row.append(label);
        }
        const candidate = make("th", "candidate-label", scenario.candidates[index].name);
        candidate.setAttribute("scope", "row");
        row.append(candidate);
        const count = summary?.counts?.[status];
        const countCell = make("td", "");
        renderValue(countCell, count, mode);
        const percentCell = make("td", "");
        const percent = mode === "run" ? summary?.percentages?.[status] : count?.percent_mean;
        renderValue(percentCell, percent, "run", null, "%");
        row.append(countCell, percentCell);
        body.append(row);
      });
    }
    groupHost.append(table(head, body));
  };
  const renderTaskTypes = (groupHost, scenario, selected, mode) => {
    groupHost.append(make("h3", "subgroup-title", "Task types and duration"));
    const summaries = scenario.candidates.map((candidate, index) => mode === "run" ? candidate.runs[selected[index]].task_types : candidate.task_types);
    const types = [...new Set(summaries.flatMap(summary => Object.keys(summary || {})))].sort();
    const fields = ["count", "duration_mean", "duration_p95", "duration_total"];
    const head = make("thead", "");
    const labels = make("tr", "");
    labels.append(make("th", "", "Variable"), make("th", "", "Candidate"));
    ["Count", "Mean duration (ms)", "p95 duration (ms)", "Total duration (ms)"].forEach(label => labels.append(make("th", "", label)));
    head.append(labels);
    const body = make("tbody", "");
    for (const [typeIndex, type] of (types.length ? types : ["No observations"]).entries()) {
      scenario.candidates.forEach((candidate, index) => {
        const row = make("tr", typeIndex % 2 ? "variable-band-alt" : "");
        if (index === 0) {
          const label = make("th", "variable-label", type);
          label.rowSpan = scenario.candidates.length;
          label.setAttribute("scope", "rowgroup");
          row.append(label);
        }
        const name = make("th", "candidate-label", candidate.name);
        name.setAttribute("scope", "row");
        row.append(name);
        const summary = summaries[index]?.[type];
        fields.forEach(field => {
          const cell = make("td", "");
          let value = summary?.[field];
          if (field === "count" && type !== "No observations" && value == null) {
            value = mode === "run" ? 0 : {mean: 0, sd: candidate.runs.length > 1 ? 0 : null, median: 0, min: 0, max: 0, n: candidate.runs.length};
          }
          renderValue(cell, value, mode);
          row.append(cell);
        });
        body.append(row);
      });
    }
    groupHost.append(table(head, body));
  };
  const renderStatistics = (host, scenario, selected, mode) => {
    host.replaceChildren();
    if (scenario.selected_stats?.length) {
      const section = make("div", "summary-group");
      section.append(make("h3", "group-heading", "Selected statistics"));
      let variables = [];
      const renderVariables = () => {
        for (const batch of selectedBatchesFor(variables)) renderGenericBatch(section, batch, scenario, selected, mode);
        variables = [];
      };
      for (const key of scenario.selected_stats) {
        if (key === "trial_phases") {
          renderVariables();
          renderSelectedPhases(section, scenario, selected, mode);
        } else {
          const variable = data.variables.find(item => item.key === key);
          if (variable) variables.push({...variable, title: `${variable.group}: ${variable.title}`});
        }
      }
      renderVariables();
      host.append(section);
    }
    const groups = [...new Set(data.variables.map(variable => variable.group))];
    for (const group of groups) {
      const section = make("div", "summary-group");
      section.append(make("h3", "group-heading", group));
      const variables = data.variables.filter(item => item.group === group);
      if (group === "Tasks") {
        renderTaskTypes(section, scenario, selected, mode);
        renderTaskOutcomes(section, scenario, selected, mode);
        for (const batch of batchesFor(variables.filter(item => !["task_type", "task_status"].includes(item.key)))) {
          renderGenericBatch(section, batch, scenario, selected, mode);
        }
      } else {
        for (const batch of batchesFor(variables)) renderGenericBatch(section, batch, scenario, selected, mode);
      }
      host.append(section);
    }
  };
  for (const scenario of data.scenarios) {
    const section = make("section", "");
    section.append(make("h2", "", scenario.name));
    const controls = make("div", "controls");
    const modeLabel = make("label", "", "View");
    const mode = make("select", "");
    mode.setAttribute("aria-label", `${scenario.name}: summary level`);
    for (const [value, label] of [["candidate", "Across runs"], ["run", "Selected run"]]) {
      const option = make("option", "", label);
      option.value = value;
      mode.append(option);
    }
    modeLabel.append(mode);
    controls.append(modeLabel);
    section.append(controls);
    const runControls = make("div", "run-controls");
    const selected = scenario.candidates.map(() => 0);
    const selectors = [];
    scenario.candidates.forEach((candidate, index) => {
      const label = make("label", "", candidate.name);
      const selector = make("select", "");
      selector.setAttribute("aria-label", `${scenario.name}: run for ${candidate.name}`);
      candidate.runs.forEach((run, runIndex) => {
        const option = make("option", "", run.run);
        option.value = String(runIndex);
        selector.append(option);
      });
      selector.addEventListener("change", () => {
        selected[index] = Number(selector.value);
        renderStatistics(host, scenario, selected, mode.value);
      });
      label.append(selector);
      if (index < shortcutKeys.length) {
        const key = shortcutKeys[index];
        label.append(make("small", "shortcut-hint", `${key} next · ${key.toUpperCase()} previous`));
      }
      selectors.push(selector);
      runControls.append(label);
    });
    runControls.hidden = true;
    section.append(runControls);
    const host = make("div", "summary-host");
    section.append(host);
    mode.addEventListener("change", () => {
      runControls.hidden = mode.value !== "run";
      renderStatistics(host, scenario, selected, mode.value);
    });
    root.append(section);
    renderStatistics(host, scenario, selected, mode.value);
    scenarioControls.push({mode, selectors});
  }
  window.addEventListener("keydown", event => {
    if (event.altKey || event.ctrlKey || event.metaKey || event.repeat) return;
    const target = event.target;
    if (target instanceof HTMLElement && (target.isContentEditable || ["INPUT", "TEXTAREA"].includes(target.tagName))) return;
    const index = shortcutKeys.indexOf(event.key.toLowerCase());
    if (index < 0) return;
    const direction = event.key === event.key.toUpperCase() ? -1 : 1;
    let changed = false;
    for (const {mode, selectors} of scenarioControls) {
      if (mode.value !== "run") continue;
      const selector = selectors[index];
      if (!selector || selector.options.length < 2) continue;
      selector.selectedIndex = (selector.selectedIndex + direction + selector.options.length) % selector.options.length;
      selector.dispatchEvent(new Event("change"));
      changed = true;
    }
    if (changed) event.preventDefault();
  }, true);
})();
</script>
__LAYOUT_MARKER__
</body>
</html>
""".replace("__LAYOUT_MARKER__", _LAYOUT_MARKER)
