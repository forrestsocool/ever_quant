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
    getBoundingClientRect() { return { left: 0, top: 0, width: this.clientWidth, height: this.clientHeight }; },
    focus() {},
    closest(selector) { return this.querySelector(selector); },
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
  const frames = new Map(); let nextFrame = 0;
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
    RadarTimeline: require("../national_team_radar/static/timeline.js"),
    requestAnimationFrame(callback) { frames.set(++nextFrame, callback); return nextFrame; },
    cancelAnimationFrame(id) { frames.delete(id); },
  };
  const injected = source.replace(/  init\(\);\s*\}\)\(\);\s*$/, "  globalThis.pulse = { state, renderMainChart, bindEvents, readUrlState, installDataset, selectTradingDate };\n})();");
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
  return { state, renderMainChart, readUrlState, get, urls, location, ...sandbox.pulse, frames,
    flushFrame() { const callbacks = [...frames.values()]; frames.clear(); callbacks.forEach((callback) => callback()); } };
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
  ui.get("mainChart").onpointermove({ clientX: 100, clientY: 100 }); ui.flushFrame();
  assert.match(ui.get("chartTooltip").innerHTML, /4,000\.00 点/);
  ui.get("mainChart").onpointermove({ clientX: 400, clientY: 100 }); ui.flushFrame();
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

test("one year and latest disclosure are the defaults; full-history daily bars do not overlap", () => {
  const ui = setup([]);
  assert.equal(ui.readUrlState().range, "252");
  assert.equal(ui.readUrlState().mode, "latest");
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

test("drag events coalesce to one frame and link all dated panels without rebuilding the chart", () => {
  const ui = setup([
    { trade_date: "2026-10-12", close: 4000, pct_chg: 1 },
    { trade_date: "2026-10-14", close: 4050, pct_chg: 1.25 },
  ]);
  const data = ui.state.snapshot;
  data.as_of = "2026-10-14";
  data.etfs = [{ ts_code: "510300.SH", name: "ETF", index: "沪深300" }];
  data.etf_series = { "510300.SH": data.series.map((row) => ({ ...row, valid: true, total_share_yi: row.flow_yi * 2, delta_share_yi: row.flow_yi / 4, share_change_pct: 1, status: "观察" })) };
  data.series.forEach((row, i) => Object.assign(row, { complete: true, valid_count: 1, total_count: 1, sync_in: 0, sync_out: 0, status: "观察", flow_5d_yi: i + 30, flow_20d_yi: i + 100 }));
  data.evidence = [{ date: "2026-10-12", title: "earlier evidence" }, { date: "2026-10-14", title: "future evidence" }];
  data.alerts = [{ date: "2026-10-12", flow_yi: 12 }, { date: "2026-10-14", flow_yi: 15 }];
  data.crosschecks = { channels: { trades: { name: "辅助记录", rows: [{ date: "2026-10-12" }, { date: "2026-10-14" }] } } };
  ui.installDataset(data); ui.renderMainChart();
  const chart = ui.get("mainChart"); const geometry = chart.innerHTML;
  assert.equal(ui.state.selectedDate, "2026-10-14");
  chart.onpointerdown({ clientX: 750, clientY: 100, pointerId: 5 });
  for (let i = 0; i < 100; i++) chart.onpointermove({ clientX: 100, clientY: 100, pointerId: 5 });
  assert.equal(ui.frames.size, 1);
  assert.equal(ui.state.selectedDate, "2026-10-14", "no work until the animation frame");
  ui.flushFrame();
  assert.equal(ui.state.selectedDate, "2026-10-12");
  assert.equal(ui.get("heroFlow").textContent, "+12.00");
  assert.equal(ui.get("flow5d").textContent, "+30.00");
  assert.equal(ui.get("flow20d").textContent, "+100.00");
  assert.equal(ui.state.snapshot.etfs[0].total_share_yi, 24);
  assert.equal(ui.state.snapshot.benchmark.close, 4000);
  assert.equal(ui.state.snapshot.series.length, 1);
  assert.equal(ui.state.dataset.series.length, 3);
  assert.equal(ui.state.snapshot.crosschecks.channels.trades.total_records, 1);
  assert.equal(ui.state.snapshot.evidence.length, 1);
  assert.doesNotMatch(ui.get("evidenceList").innerHTML, /future evidence/);
  assert.equal(chart.innerHTML, geometry, "dragging must preserve all bars, paths and event surface");
  assert.equal(ui.urls.length, 0, "URL writes wait for pointer release");
  chart.onpointerup({ pointerId: 5 });
  assert.match(ui.urls.at(-1), /date=2026-10-12/);
  assert.match(ui.get("exportButton").href, /review_date=2026-10-12/);
  const cursor = chart.querySelector(".pulse-cursor").getAttribute("transform");
  chart.onpointerleave();
  assert.equal(chart.querySelector(".pulse-cursor").getAttribute("transform"), cursor);
  chart.onkeydown({ key: "ArrowRight", preventDefault() {} });
  assert.equal(ui.state.selectedDate, "2026-10-13");
  assert.equal(ui.state.snapshot.benchmark, null, "a missing benchmark date stays missing");
  chart.onkeydown({ key: "End", preventDefault() {} });
  assert.equal(ui.state.selectedDate, "2026-10-14");
  assert.equal(ui.state.snapshot.evidence.length, 2);
  assert.equal(chart.innerHTML, geometry);
});

test("one year means a calendar year rather than an assumed number of trading days", () => {
  const ui = setup([]);
  ui.state.range = "252";
  ui.state.snapshot.series = [
    { date: "2025-10-14", flow_yi: 1 },
    { date: "2025-10-15", flow_yi: 2 },
    { date: "2026-10-14", flow_yi: 3 },
  ];
  ui.renderMainChart();
  assert.equal(ui.get("chartDays").textContent, "2 个交易日");
});
