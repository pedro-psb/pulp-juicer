const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

class Element {
  constructor(tag) {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.options = [];
    this.selectedIndex = 0;
    this.listeners = {};
    this._text = "";
    this.isContentEditable = false;
  }
  get textContent() {
    return this._text + this.children.map(child => child.textContent || "").join("");
  }
  set textContent(value) { this._text = value; this.children = []; }
  get childElementCount() { return this.children.filter(child => child instanceof Element).length; }
  append(...items) {
    for (const item of items) {
      this.children.push(item);
      if (this.tagName === "SELECT" && item.tagName === "OPTION") this.options.push(item);
    }
  }
  replaceChildren() { this.children = []; this._text = ""; }
  querySelectorAll() { return []; }
  setAttribute(name, value) { if (name === "aria-label") this.ariaLabel = value; }
  addEventListener(name, handler) { this.listeners[name] = handler; }
  dispatchEvent(event) { this.listeners[event.type]?.(event); }
  get value() { return this.tagName === "SELECT" ? this.options[this.selectedIndex]?.value || "" : this._value || ""; }
  set value(value) {
    if (this.tagName === "SELECT") this.selectedIndex = this.options.findIndex(option => option.value === value);
    else this._value = value;
  }
}

const sample = (mean, total, type, duration) => ({
  run: `run${total}`,
  statistics: {
    latency: {n: 2, mean},
    bytes: {n: 2, mean: mean * 10, total: mean * 20},
    failures: {n: 2, total: 0, percent: 0},
    completions: {n: 2, mean: 1, total: 2},
    task_wait: {n: 1, mean: duration},
    task_execution: {n: 1, mean: duration * 2, total: duration * 2},
    task_status: {counts: {completed: 1}, percentages: {completed: 100}},
    phase_0: {total: total * 2},
    phase_1: {total},
  },
  task_types: {[type]: {count: 1, duration_mean: duration, duration_p95: duration, duration_total: duration}},
});
const dist = mean => ({mean, sd: 1, median: mean, min: mean - 1, max: mean + 1, n: 2});
const candidate = (name, first, second, type, duration) => ({
  name,
  runs: [sample(first, first, type, duration), sample(second, second, type, duration + 1)],
  statistics: {
    latency: {n: dist(2), mean: dist((first + second) / 2)},
    bytes: {n: dist(2), mean: dist((first + second) * 5), total: dist((first + second) * 10)},
    failures: {n: dist(2), total: dist(0), percent: dist(0)},
    completions: {n: dist(2), mean: dist(1), total: dist(2)},
    task_wait: {n: dist(1), mean: dist(duration + .5)},
    task_execution: {n: dist(1), mean: dist(duration * 2 + 1), total: dist(duration * 2 + 1)},
    task_status: {counts: {completed: {...dist(1), percent_mean: 100}}},
    phase_0: {total: dist(first + second)},
    phase_1: {total: dist((first + second) / 2)},
  },
  task_types: {[type]: {
    count: dist(1), duration_mean: dist(duration + .5),
    duration_p95: dist(duration + .5), duration_total: dist(duration + .5),
  }},
});
const data = {
  variables: [
    {group: "Requests", key: "latency", title: "Latency", unit: "request", measure: "ms", fields: ["n", "mean"]},
    {group: "Requests", key: "bytes", title: "Bytes", unit: "request", measure: "bytes", fields: ["n", "mean", "total"]},
    {group: "Requests", key: "failures", title: "Failures", unit: "request", measure: "%", fields: ["n", "total", "percent"]},
    {group: "Requests", key: "completions", title: "Completed requests", unit: "one-second bin", measure: "requests/s", fields: ["n", "mean", "total"]},
    {group: "Tasks", key: "task_wait", title: "Queue wait", unit: "task", measure: "ms", fields: ["n", "mean"]},
    {group: "Tasks", key: "task_execution", title: "Execution time", unit: "task", measure: "ms", fields: ["n", "mean", "total"]},
    {group: "Tasks", key: "task_status", title: "Task outcomes", unit: "task", measure: "count / %", fields: ["counts"]},
    {group: "Tasks", key: "task_type", title: "Task types", unit: "task", measure: "count / %", fields: ["counts"]},
    {group: "Trial phases", key: "phase_0", title: "Total duration", unit: "run", measure: "s", fields: ["total"]},
    {group: "Trial phases", key: "phase_1", title: "Preparation", unit: "run", measure: "s", fields: ["total"]},
  ],
  scenarios: [{name: "scenario", selected_stats: ["trial_phases", "latency", "failures", "completions", "task_execution"], candidates: [
    candidate("normal", 10, 11, "pull", 2),
    candidate("staging", 20, 21, "sync", 4),
  ]}],
};
const created = [];
const root = new Element("div");
const handlers = {};
const document = {
  getElementById(id) { return id === "report-data" ? {textContent: JSON.stringify(data)} : root; },
  createElement(tag) { const element = new Element(tag); created.push(element); return element; },
  createTextNode(text) { return {textContent: text}; },
};
const window = {addEventListener(name, handler) { handlers[name] = handler; }};
class Event { constructor(type) { this.type = type; } }
vm.runInNewContext(fs.readFileSync(0, "utf8"), {document, window, HTMLElement: Element, Event});

function descendants(node, predicate) {
  return node.children.flatMap(child => child instanceof Element
    ? [...(predicate(child) ? [child] : []), ...descendants(child, predicate)] : []);
}
const tables = () => descendants(root, element => element.tagName === "TABLE");
const selectedTables = () => descendants(
  descendants(root, element => element.className === "summary-group")[0],
  element => element.tagName === "TABLE",
);
assert.deepEqual(
  descendants(root, element => element.className === "group-heading").map(element => element.textContent),
  ["Selected statistics", "Requests", "Tasks", "Trial phases"],
);
assert.equal(descendants(root, element => element.tagName === "DETAILS").length, 0);
const headers = table => descendants(descendants(table, element => element.tagName === "THEAD")[0], element => element.tagName === "TR")
  .map(row => descendants(row, element => element.tagName === "TH").map(cell => cell.textContent));
const rows = table => descendants(descendants(table, element => element.tagName === "TBODY")[0], element => element.tagName === "TR")
  .map(row => descendants(row, element => ["TH", "TD"].includes(element.tagName)).map(cell => cell.textContent));
assert.equal(headers(selectedTables()[0])[0][0], "Candidate");
assert.match(rows(selectedTables()[1])[0][0], /^Requests: Latency/);
assert.match(rows(selectedTables()[2])[0][0], /^Requests: Failures/);
assert.match(rows(selectedTables()[3])[0][0], /^Requests: Completed requests/);
assert.match(rows(selectedTables()[3])[2][0], /^Tasks: Execution time/);
const rowClasses = table => descendants(descendants(table, element => element.tagName === "TBODY")[0], element => element.tagName === "TR")
  .map(row => row.className);
const generic = () => tables().find(table => rows(table)[0]?.[0]?.startsWith("Latency"));
const taskTypes = () => tables().find(table => rows(table)[0]?.[0] === "pull");
const taskMetrics = () => tables().find(table => rows(table)[0]?.[0]?.startsWith("Queue wait"));
const taskOutcomes = () => tables().find(table => rows(table)[0]?.[0] === "completed");
const phases = () => tables().find(table => headers(table)[0]?.[0] === "Candidate" && headers(table)[0]?.[1] === "Total duration (s)");
const mode = created.find(element => element.ariaLabel?.includes("summary level"));
assert.equal(mode.value, "candidate");
assert.equal(descendants(root, element => element.className === "run-controls")[0].hidden, true);
assert.equal(rows(phases())[0][1], "21 ± 1");
mode.value = "run";
mode.dispatchEvent({type: "change"});
assert.equal(descendants(root, element => element.className === "run-controls")[0].hidden, false);
assert.deepEqual(headers(phases())[0], ["Candidate", "Total duration (s)", "Preparation (s)"]);
assert.deepEqual(rows(phases()), [["normal", "20", "10"], ["staging", "40", "20"]]);
assert.ok(selectedTables().some(table => rows(table).some(row => row[0]?.startsWith("Tasks: Execution time"))));
assert.deepEqual(headers(generic())[0], ["Variable", "Candidate", "n", "mean", "total"]);
assert.deepEqual(rows(generic()), [
  ["Latencyrequest · msn = measured requests", "normal", "2", "10", "N/A"],
  ["staging", "2", "20", "N/A"],
  ["Bytesrequest · bytesn = measured requests", "normal", "2", "100", "200"],
  ["staging", "2", "200", "400"],
  ["Completed requestsone-second bin · requests/sn = one-second intervals", "normal", "2", "1", "2"],
  ["staging", "2", "1", "2"],
]);
assert.equal(descendants(generic(), element => element.className === "variable-label")[0].rowSpan, 2);
assert.deepEqual(rowClasses(generic()), ["", "", "variable-band-alt", "variable-band-alt", "", ""]);
assert.deepEqual(headers(taskMetrics())[0], ["Variable", "Candidate", "n", "mean", "total"]);
assert.deepEqual(rows(taskMetrics())[0], ["Queue waittask · msn = measured tasks", "normal", "1", "2", "N/A"]);
assert.deepEqual(rows(taskMetrics())[1], ["staging", "1", "4", "N/A"]);
assert.deepEqual(rowClasses(taskMetrics()), ["", "", "variable-band-alt", "variable-band-alt"]);
assert.deepEqual(headers(taskTypes())[0], [
  "Variable", "Candidate", "Count", "Mean duration (ms)", "p95 duration (ms)", "Total duration (ms)",
]);
assert.deepEqual(rows(taskTypes())[0], ["pull", "normal", "1", "2", "2", "2"]);
assert.deepEqual(rows(taskTypes())[1], ["staging", "0", "N/A", "N/A", "N/A"]);
assert.equal(descendants(taskTypes(), element => element.className === "variable-label")[0].rowSpan, 2);
assert.deepEqual(headers(taskOutcomes())[0], ["Variable", "Candidate", "Count", "%"]);
assert.deepEqual(rows(taskOutcomes()), [
  ["completed", "normal", "1", "100%"],
  ["staging", "1", "100%"],
]);
const selectors = created.filter(element => element.ariaLabel?.includes("run for"));
selectors[0].value = "1";
selectors[0].dispatchEvent({type: "change"});
assert.deepEqual(rows(generic())[0], ["Latencyrequest · msn = measured requests", "normal", "2", "11", "N/A"]);
assert.deepEqual(rows(phases())[0], ["normal", "22", "11"]);
mode.value = "candidate";
mode.dispatchEvent({type: "change"});
assert.match(rows(generic())[0][0], /n = measured requests per run$/);
assert.match(rows(taskMetrics())[0][0], /n = measured tasks per run$/);
assert.equal(rows(generic())[0][3], "10.5 ± 1");
assert.equal(rows(taskTypes())[0][3], "2.5 ± 1");
assert.equal(rows(phases())[0][1], "21 ± 1");
assert.equal(descendants(root, element => element.className === "value-detail").length, 0);
assert.equal(descendants(root, element => element.className === "value-main").length, 0);
const press = (key, target, extra = {}) => handlers.keydown({
  key, target, altKey: false, ctrlKey: false, metaKey: false, repeat: false,
  preventDefault() {}, ...extra,
});
press("a", selectors[0]);
assert.equal(selectors[0].selectedIndex, 1);
mode.value = "run";
mode.dispatchEvent({type: "change"});
assert.deepEqual(
  descendants(root, element => element.className === "shortcut-hint").map(element => element.textContent),
  ["a next · A previous", "s next · S previous"],
);
press("a", selectors[0]);
assert.equal(selectors[0].selectedIndex, 0);
assert.equal(rows(generic())[0][3], "10");
press("A", selectors[0]);
assert.equal(selectors[0].selectedIndex, 1);
assert.equal(rows(generic())[0][3], "11");
press("S", selectors[0]);
assert.equal(selectors[1].selectedIndex, 1);
press("s", selectors[0]);
assert.equal(selectors[1].selectedIndex, 0);
press("a", new Element("input"));
press("a", selectors[0], {ctrlKey: true});
press("f", selectors[0]);
assert.deepEqual(selectors.map(selector => selector.selectedIndex), [1, 0]);
