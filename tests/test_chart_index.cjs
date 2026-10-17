"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "..", "national_team_radar", "static", "app.js"), "utf8");

function element(id) {
  const attributes = new Map();
  const listeners = new Map();
  const children = new Map();
  return {
    id, innerHTML: "", textContent: "", value: "", style: {}, hidden: false,
    disabled: false, clientWidth: 800, clientHeight: 340, dataset: {},
    classList: { add() {}, remove() {}, toggle() {} },
    setAttribute(name, value) { attributes.set(name, String(value)); },
    getAttribute(name) { return attributes.get(name); },
    addEventListener(name, callback) { listeners.set(name, callback); },
    fire(name, event = {}) { listeners.get(name)?.(event); },
    querySelector(selector) {
      if (!children.has(selector)) children.set(selector, element(`${id} ${selector}`));
      return children.get(selector);
    },
    querySelectorAll(selector) {
      if (selector !== ".hit-zone") return [];
      this.hitZones = [...this.innerHTML.matchAll(/class="hit-zone" data-index="(\d+)"/g)].map((match) => {
        const zone = element("hit-zone");
        zone.dataset.index = match[1];
        return zone;
      });
      return this.hitZones;
    },
    getBoundingClientRect() { return { left: 0, top: 0, width: 200, height: 80 }; },
  };
}

function setup(benchmark) {
  const elements = new Map();
  const get = (id) => {
    if (!elements.has(id)) elements.set(id, element(id));
    return elements.get(id);
  };
  const location = { pathname: "/", search: "", hash: "#overview" };
  const urls = [];
  const sandbox = {
    document: {
      getElementById: get, querySelector: get, querySelectorAll: () => [],
      addEventListener() {}, createElement: element, body: element("body"),
    },
    window: {
      location, innerWidth: 1100, innerHeight: 700,
      history: { replaceState(_state, _title, url) { urls.push(url); } },
      addEventListener() {},
    },
    URL, URLSearchParams, Intl, setTimeout() {}, clearTimeout() {},
  };
  const injected = source.replace(/  init\(\);\s*\}\)\(\);\s*$/, "  globalThis.pulse = { state, renderMainChart, bindEvents, readUrlState };\n})();");
  assert.notEqual(injected, source);
  vm.createContext(sandbox);
  vm.runInContext(injected, sandbox);
  const { state, renderMainChart, bindEvents, readUrlState } = sandbox.pulse;
  state.params = { window: 20, z_threshold: 2, min_flow_yi: 10, sync_min: 3, start_date: "2026-10-12", end_date: "2026-10-14" };
  state.snapshot = {
    summary: { total_count: 7 },
    series: [
      { date: "2026-10-12", flow_yi: 12, z_score: 2.4, valid_count: 7 },
      { date: "2026-10-13", flow_yi: -7, z_score: -1.1, valid_count: 7 },
      { date: "2026-10-14", flow_yi: 15, z_score: 2.7, valid_count: 7 },
    ],
    metadata: { benchmark },
  };
  state.range = "all";
  bindEvents();
  return { state, renderMainChart, readUrlState, get, urls, location };
}

test("CSI 300 uses a distinct right axis and does not bridge missing dates", () => {
  const ui = setup([
    { trade_date: "2026-10-12", close: 4000, pct_chg: 1 },
    { trade_date: "2026-10-14", close: 4050, pct_chg: 1.25 },
  ]);
  ui.renderMainChart();
  const svg = ui.get("mainChart").innerHTML;
  const pathData = svg.match(/class="index-path" d="([^"]+)"/)?.[1];
  assert.ok(pathData);
  assert.equal((pathData.match(/M/g) ?? []).length, 2);
  assert.equal((pathData.match(/L/g) ?? []).length, 0);
  assert.match(svg, /沪深300 · 点/);
  assert.match(svg, /资金 · 亿元/);
  assert.match(svg, /Z 异常度/);
  assert.match(svg, /class="z-path"/);
  assert.equal(ui.get("indexToggle").getAttribute("aria-pressed"), "true");
  ui.get("mainChart").hitZones[0].fire("pointermove", { clientX: 100, clientY: 100 });
  assert.match(ui.get("chartTooltip").innerHTML, /4,000\.00 点/);
  ui.get("mainChart").hitZones[1].fire("pointermove", { clientX: 100, clientY: 100 });
  assert.match(ui.get("chartTooltip").innerHTML, /沪深300<\/span><strong>当日缺失/);
});

test("index toggle removes both the line and its axis and persists in the URL", () => {
  const ui = setup([{ trade_date: "2026-10-12", close: 4000, pct_chg: 1 }]);
  ui.renderMainChart();
  ui.get("indexToggle").fire("click");
  assert.equal(ui.state.indexVisible, false);
  assert.doesNotMatch(ui.get("mainChart").innerHTML, /class="index-path"/);
  assert.doesNotMatch(ui.get("mainChart").innerHTML, /沪深300 · 点/);
  assert.match(ui.get("mainChart").innerHTML, /class="z-path"/);
  assert.equal(ui.get("indexToggle").getAttribute("aria-pressed"), "false");
  assert.match(ui.urls.at(-1), /index=off/);
  ui.location.search = "?index=off";
  assert.equal(ui.readUrlState().indexVisible, false);
  ui.get("indexToggle").fire("click");
  assert.match(ui.get("mainChart").innerHTML, /class="index-path"/);
});

test("missing index series leaves the fund chart useful and marks the toggle unavailable", () => {
  const ui = setup([]);
  ui.renderMainChart();
  assert.match(ui.get("mainChart").innerHTML, /class="bar inflow"/);
  assert.match(ui.get("mainChart").innerHTML, /class="z-path"/);
  assert.doesNotMatch(ui.get("mainChart").innerHTML, /class="index-path"/);
  assert.equal(ui.get("indexToggle").disabled, true);
  assert.equal(ui.get("indexToggleState").textContent, "暂无数据");
  assert.equal(ui.state.indexVisible, true);
});

test("full history is the default and dense daily bars do not overlap adjacent sessions", () => {
  const ui = setup([]);
  assert.equal(ui.readUrlState().range, "all");
  ui.location.search = "?range=1260";
  assert.equal(ui.readUrlState().range, "1260");
  ui.state.snapshot.series = Array.from({ length: 3000 }, (_, i) => ({ date: new Date(Date.UTC(2016, 10, 4 + i)).toISOString().slice(0, 10), flow_yi: i % 2 ? -5 : 10, z_score: .5, valid_count: 7 }));
  ui.state.snapshot.metadata.history = { common_listing_date: "2016-11-04", complete_days: 2990, trading_days: 3000, first_z_date: "2016-12-05" };
  ui.renderMainChart();
  const svg = ui.get("mainChart").innerHTML;
  const widths = [...svg.matchAll(/class="bar [^"]+"[^>]*width="([^"]+)"/g)].map((match) => Number(match[1]));
  assert.equal(widths.length, 3000);
  assert.ok(widths.every((width) => width < .25));
  assert.match(ui.get("historySummary").textContent, /2016-11-04/);
  assert.match(ui.get("historySummary").textContent, /2990 \/ 3000/);
});
