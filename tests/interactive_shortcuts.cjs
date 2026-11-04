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
    this.style = {};
    this.isContentEditable = false;
  }
  append(...items) {
    for (const item of items) {
      this.children.push(item);
      if (this.tagName === "SELECT" && item.tagName === "OPTION") this.options.push(item);
    }
  }
  setAttribute(name, value) { if (name === "aria-label") this.ariaLabel = value; }
  addEventListener(name, handler) { this.listeners[name] = handler; }
  dispatchEvent(event) { this.listeners[event.type]?.(event); }
  get value() { return this.options[this.selectedIndex]?.value || ""; }
  set value(value) { this.selectedIndex = this.options.findIndex(option => option.value === value); }
}

const created = [];
const root = new Element("div");
const handlers = {};
const candidates = Array.from({length: 5}, (_, index) => ({
  name: `candidate-${index + 1}`,
  runs: Array.from({length: 3}, (_, run) => ({
    run: `run00${run + 1}`, series: {}, markers: null,
  })),
}));
const data = {
  scenarios: ["one", "two"].map(name => ({name, candidates})),
  groups: [],
};
const document = {
  getElementById(id) {
    return id === "report-data" ? {textContent: JSON.stringify(data)} : root;
  },
  createElement(tag) {
    const element = new Element(tag);
    created.push(element);
    return element;
  },
};
const window = {
  addEventListener(name, handler) { handlers[name] = handler; },
};
class Event { constructor(type) { this.type = type; } }
vm.runInNewContext(fs.readFileSync(0, "utf8"), {
  document, window, uPlot: function () {}, HTMLElement: Element, Event,
  clearTimeout() {}, setTimeout() {},
});

const selectors = created.filter(element =>
  element.tagName === "SELECT" && element.ariaLabel?.includes("run for")
);
const values = () => [
  selectors.slice(0, 5).map(select => select.selectedIndex),
  selectors.slice(5).map(select => select.selectedIndex),
];
const press = (key, target) => handlers.keydown({
  key, target, altKey: false, ctrlKey: false, metaKey: false, repeat: false,
  preventDefault() {},
});
const same = expected => assert.deepEqual(values(), [expected, expected]);
press("a", selectors[0]);
same([1, 0, 0, 0, 0]);
press("A", selectors[0]);
same([0, 0, 0, 0, 0]);
press("S", selectors[0]);
same([0, 2, 0, 0, 0]);
press("f", selectors[0]);
press("g", selectors[0]);
same([0, 2, 0, 1, 0]);
press("a", new Element("input"));
same([0, 2, 0, 1, 0]);
