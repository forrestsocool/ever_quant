"use strict";

// Exercise the shipped browser controller without adding a production-only test API.
// These fakes provide DOM boundaries; all view commits and asynchronous control flow
// below run through the real app.js functions.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const controllerPath = path.join(__dirname, "..", "national_team_radar", "static", "app.js");

function element(name) {
  const children = new Map();
  const attributes = new Map();
  return {
    id: name, value: "", textContent: "", innerHTML: "", className: "",
    hidden: false, disabled: false, open: false, href: "", title: "",
    clientWidth: 800, clientHeight: 280, dataset: {}, style: {}, options: [],
    classList: { add() {}, remove() {}, toggle() {} },
    setAttribute(key, value) { attributes.set(key, value); },
    getAttribute(key) { return attributes.get(key); },
    addEventListener() {}, focus() {}, scrollIntoView() {},
    insertAdjacentElement() {}, appendChild(child) { this.options.push(child); },
    querySelector(selector) {
      if (!children.has(selector)) children.set(selector, element(`${name} ${selector}`));
      return children.get(selector);
    },
    querySelectorAll() { return []; },
    closest(selector) { return this.querySelector(selector); },
    getBoundingClientRect() { return { left: 0, top: 0, right: 800, bottom: 280, width: 800, height: 280 }; },
  };
}

function fixture(date, { complete = true, lastComplete = null, flow = 12 } = {}) {
  return {
    as_of: date,
    summary: {
      date, complete, valid_count: complete ? 7 : 1, total_count: 7,
      flow_yi: complete ? flow : null, status: complete ? "观察" : "数据不足",
      z_score: complete ? .5 : null, sync_in: 0, sync_out: 0,
      flow_5d_yi: null, flow_20d_yi: null,
    },
    last_complete: lastComplete ? { date: lastComplete, flow_yi: 10 } : null,
    series: [], etfs: [], alerts: [], evidence: [], etf_series: {},
    metadata: { requested_start_date: "2025-01-01", requested_end_date: date },
    quality: { warnings: [] },
  };
}

function deferred() {
  let resolve;
  const promise = new Promise((finish) => { resolve = finish; });
  return { promise, resolve };
}

function harness(handler) {
  const elements = new Map();
  const get = (id) => {
    if (!elements.has(id)) elements.set(id, element(id));
    return elements.get(id);
  };
  const document = {
    getElementById: get,
    querySelector: get,
    querySelectorAll: () => [],
    createElement: element,
    addEventListener() {},
    body: element("body"),
  };
  const requests = [];
  const history = [];
  const location = { pathname: "/", search: "", hash: "#overview" };
  const sandbox = {
    document, URL, URLSearchParams, Intl, RadarTimeline: require("../national_team_radar/static/timeline.js"),
    window: {
      location, innerWidth: 1200, innerHeight: 800, addEventListener() {},
      history: { replaceState(_state, _title, url) { history.push(url); } },
    },
    setTimeout: () => 1, clearTimeout() {},
    async fetch(url, options) {
      requests.push({ url, options });
      const result = await handler(new URL(url, "http://localhost"), options, requests.length);
      if (result?.httpError) return { ok: false, status: result.httpError, json: async () => ({ detail: result.message }) };
      return { ok: true, status: 200, json: async () => result };
    },
  };
  const source = fs.readFileSync(controllerPath, "utf8");
  const injected = source.replace(/  init\(\);\s*\}\)\(\);\s*$/, "  globalThis.controller = { state, loadSnapshot, pollStatus, syncDateInputs, loadHistory, selectTradingDate, installDataset };\n})();");
  assert.notEqual(injected, source, "controller initialization seam must match app.js");
  vm.createContext(sandbox);
  vm.runInContext(injected, sandbox, { filename: controllerPath });
  const { state, loadSnapshot, pollStatus, syncDateInputs } = sandbox.controller;
  state.config = { start_date: "2025-01-01", today: "2026-10-16" };
  state.baseStart = "2025-01-01";
  state.params = { window: 20, z_threshold: 2, min_flow_yi: 10, sync_min: 3, start_date: "2025-01-01", end_date: "2026-10-14" };
  state.snapshot = fixture("2026-10-14");
  state.latestSnapshot = fixture("2026-10-15", { complete: false, lastComplete: "2026-10-14" });
  state.loaded = true;
  syncDateInputs(state.snapshot);
  return { state, loadSnapshot, pollStatus, get, requests, history, ...sandbox.controller };
}

test("failed query preserves the displayed dates, thresholds, data and export", async () => {
  const ui = harness(async () => ({ httpError: 503, message: "fixture data unavailable" }));
  ui.state.mode = "custom";
  const original = ui.state.snapshot;
  const exportUrl = ui.get("exportButton").href;
  ui.get("startDate").value = "2026-10-01";
  ui.get("endDate").value = "2026-10-12";
  const candidate = { ...ui.state.params, start_date: "2026-10-01", end_date: "2026-10-12", min_flow_yi: 5 };

  assert.equal(await ui.loadSnapshot({ mode: "custom", params: candidate }), false);
  assert.equal(ui.state.snapshot, original);
  assert.equal(ui.state.params.start_date, "2025-01-01");
  assert.equal(ui.state.params.end_date, "2026-10-14");
  assert.equal(ui.state.params.min_flow_yi, 10);
  assert.equal(ui.get("startDate").value, "2025-01-01");
  assert.equal(ui.get("endDate").value, "2026-10-14");
  assert.equal(ui.get("exportButton").href, exportUrl);
  assert.equal(ui.get("dateApply").disabled, false);
  assert.match(ui.get("noticeText").textContent, /日期与导出均已保留/);
  assert.equal(ui.history.length, 0);
});

test("a late response cannot replace the user's newer view or export", async () => {
  const oldResponse = deferred();
  const newResponse = deferred();
  const ui = harness(async (url) => url.searchParams.get("end_date") === "2026-10-12" ? oldResponse.promise : newResponse.promise);
  const oldRequest = ui.loadSnapshot({ mode: "custom", params: { ...ui.state.params, end_date: "2026-10-12", min_flow_yi: 5 } });
  const newRequest = ui.loadSnapshot({ mode: "custom", params: { ...ui.state.params, end_date: "2026-10-13", min_flow_yi: 8 } });
  newResponse.resolve(fixture("2026-10-13", { flow: 33 }));
  assert.equal(await newRequest, true);
  const exportUrl = ui.get("exportButton").href;
  oldResponse.resolve(fixture("2026-10-12", { flow: -99 }));
  await oldRequest;

  assert.equal(ui.state.snapshot.as_of, "2026-10-13");
  assert.equal(ui.get("heroFlow").textContent, "+33.00");
  assert.equal(ui.state.params.min_flow_yi, 8);
  assert.equal(ui.get("endDate").value, "2026-10-13");
  assert.equal(ui.get("exportButton").href, exportUrl);
  assert.match(exportUrl, /end_date=2026-10-13/);
  assert.equal(ui.history.length, 1);
});

test("complete mode rechecks NAV-cutoff completeness until an actually complete date", async () => {
  const ui = harness(async (url) => {
    const end = url.searchParams.get("end_date");
    if (end === "2026-10-16") return fixture("2026-10-15", { complete: false, lastComplete: "2026-10-14" });
    if (end === "2026-10-14") return fixture("2026-10-14", { complete: false, lastComplete: "2026-10-13" });
    if (end === "2026-10-13") return fixture("2026-10-13", { flow: -21 });
    throw new Error(`unexpected cutoff ${end}`);
  });

  assert.equal(await ui.loadSnapshot({ mode: "complete" }), true);
  assert.deepEqual(ui.requests.map(({ url }) => new URL(url, "http://localhost").searchParams.get("end_date")), ["2026-10-16", "2026-10-14", "2026-10-13"]);
  assert.equal(ui.state.snapshot.as_of, "2026-10-13");
  assert.equal(ui.state.snapshot.summary.complete, true);
  assert.equal(ui.state.params.end_date, "2026-10-13");
  assert.equal(ui.state.latestSnapshot.as_of, "2026-10-15");
  assert.match(ui.get("latestProgress").innerHTML, /2026-10-15/);
  assert.match(ui.get("exportButton").href, /end_date=2026-10-13/);
});

test("a completed refresh reloads the view even when market updated_at is unchanged", async () => {
  let statusReads = 0;
  const ui = harness(async (url) => {
    if (url.pathname === "/api/status") return {
      running: false, stage: "complete", updated_at: "2026-10-15T10:30:00+08:00",
      completed_at: ++statusReads === 1 ? "2026-10-16T08:30:00+08:00" : "2026-10-16T09:00:00+08:00",
    };
    if (url.pathname === "/api/config") return { start_date: "2025-01-01", today: "2026-10-16" };
    if (url.pathname === "/api/snapshot") return fixture("2026-10-16", { flow: 48 });
    throw new Error(`unexpected request ${url}`);
  });
  ui.state.mode = "latest";
  await ui.pollStatus();
  assert.equal(ui.requests.filter(({ url }) => url.startsWith("/api/snapshot")).length, 0);
  await ui.pollStatus();

  assert.equal(ui.state.snapshot.as_of, "2026-10-16");
  assert.equal(ui.get("heroFlow").textContent, "+48.00");
  assert.equal(ui.requests.filter(({ url }) => url.startsWith("/api/snapshot")).length, 1);
  assert.equal(ui.state.newDataAt, "2026-10-16T09:00:00+08:00");
});

test("background updates refresh disclosure progress while retaining a historical end date", async () => {
  let statusReads = 0;
  const ui = harness(async (url) => {
    if (url.pathname === "/api/status") return { running: false, stage: "complete", updated_at: `update-${++statusReads}` };
    if (url.pathname === "/api/config") return { start_date: "2025-01-01", today: "2026-10-16" };
    if (url.pathname === "/api/snapshot") return fixture(url.searchParams.get("end_date"), { flow: 27 });
    throw new Error(`unexpected request ${url}`);
  });
  ui.state.mode = "custom";
  ui.state.params.end_date = "2026-10-01";
  ui.state.snapshot = fixture("2026-10-01", { flow: 25 });
  await ui.pollStatus();
  await ui.pollStatus();

  assert.equal(ui.state.mode, "custom");
  assert.equal(ui.state.params.end_date, "2026-10-01");
  assert.equal(ui.state.snapshot.as_of, "2026-10-01");
  assert.equal(ui.state.snapshot.summary.flow_yi, 27);
  assert.equal(ui.state.latestSnapshot.as_of, "2026-10-16");
  assert.equal(ui.get("endDate").value, "2026-10-01");
  assert.match(ui.get("exportButton").href, /end_date=2026-10-01/);
  assert.match(ui.history.at(-1), /view=custom/);
  assert.match(ui.history.at(-1), /end_date=2026-10-01/);
});

test("a quiet update preserves an editor opened and edited while its request was pending", async () => {
  const response = deferred();
  const ui = harness(async () => response.promise);
  ui.state.mode = "latest";
  const loading = ui.loadSnapshot({ quiet: true, mode: "latest" });
  ui.state.dateEditorOpen = true;
  ui.state.dateDraftDirty = true;
  ui.get("startDate").value = "2026-09-01";
  ui.get("endDate").value = "2026-10-13";
  response.resolve(fixture("2026-10-16"));
  await loading;
  assert.equal(ui.state.dateEditorOpen, true);
  assert.equal(ui.state.dateDraftDirty, true);
  assert.equal(ui.get("customDateEditor").hidden, false);
  assert.equal(ui.get("endDate").value, "2026-10-13");
  assert.match(ui.get("exportButton").href, /end_date=2026-10-16/);
  assert.match(ui.get("dateDraftNote").textContent, /尚未应用/);
});

test("a quiet update failure leaves an unapplied date draft intact", async () => {
  const ui = harness(async () => ({ httpError: 503, message: "暂不可用" }));
  ui.state.snapshot = fixture("2026-10-14");
  ui.state.dateEditorOpen = true;
  ui.state.dateDraftDirty = true;
  ui.get("endDate").value = "2026-10-13";
  await ui.loadSnapshot({ quiet: true });
  assert.equal(ui.state.dateEditorOpen, true);
  assert.equal(ui.state.dateDraftDirty, true);
  assert.equal(ui.get("endDate").value, "2026-10-13");
});

test("complete mode migrates old bookmarks to the common pool start while custom dates remain explicit", async () => {
  const ui = harness(async () => fixture("2026-10-16"));
  ui.state.config.history_start_date = "2016-11-04";
  await ui.loadSnapshot({ mode: "complete" });
  assert.match(ui.requests[0].url, /start_date=2016-11-04/);
  assert.equal(ui.state.params.start_date, "2016-11-04");
  const candidate = { ...ui.state.params, start_date: "2020-01-01", end_date: "2020-12-31" };
  await ui.loadSnapshot({ mode: "custom", params: candidate });
  assert.match(ui.requests.at(-1).url, /start_date=2020-01-01/);
  assert.equal(ui.state.params.start_date, "2020-01-01");
});

function timelineFixture(dates) {
  const snapshot = fixture(dates.at(-1));
  snapshot.series = dates.map((date, i) => ({ date, complete: true, flow_yi: i + 1, valid_count: 1, total_count: 1, z_score: .5, status: "观察", sync_in: 0, sync_out: 0 }));
  snapshot.summary = snapshot.series.at(-1);
  snapshot.etfs = [{ ts_code: "510300.SH", name: "ETF" }];
  snapshot.etf_series = { "510300.SH": snapshot.series.map((row) => ({ ...row, valid: true, total_share_yi: 10 })) };
  return snapshot;
}

test("recent data becomes usable before full history; background completion retains the selected day", async () => {
  const older = deferred();
  const recentDates = ["2026-10-14", "2026-10-15", "2026-10-16"];
  const ui = harness(async (url) => url.searchParams.get("scope") === "all" ? older.promise : timelineFixture(recentDates));
  ui.state.progressive = true; ui.state.latestSnapshot = null;
  ui.state.config.history_start_date = "2016-11-04";
  assert.equal(await ui.loadSnapshot({ mode: "latest" }), true);
  assert.equal(ui.requests.length, 1);
  assert.match(ui.requests[0].url, /^\/api\/dashboard\?/);
  assert.match(ui.requests[0].url, /scope=year/);
  assert.equal(ui.state.selectedDate, "2026-10-16");
  assert.equal(ui.state.loading, false);
  assert.equal(ui.state.historyReady, false);
  const background = ui.loadHistory();
  assert.equal(ui.state.historyLoading, true);
  assert.equal(ui.selectTradingDate("2026-10-15"), true);
  const requestCount = ui.requests.length;
  older.resolve(timelineFixture(["2016-11-04", ...recentDates]));
  await background;
  assert.equal(ui.state.historyReady, true);
  assert.equal(ui.state.historyLoading, false);
  assert.equal(ui.state.dataset.series.length, 4);
  assert.equal(ui.state.selectedDate, "2026-10-15");
  assert.equal(ui.state.snapshot.as_of, "2026-10-15");
  assert.equal(ui.state.params.start_date, "2016-11-04");
  ui.selectTradingDate("2026-10-14");
  assert.equal(ui.requests.length, requestCount, "date selection must never fetch");
});

test("background history defers its commit during pointer capture and ignores obsolete responses", async () => {
  const response = deferred();
  const ui = harness(async () => response.promise);
  ui.state.progressive = true;
  ui.installDataset(timelineFixture(["2026-10-14", "2026-10-15"]));
  const current = ui.state.dataset;
  ui.state.dragging = true;
  const loading = ui.loadHistory();
  response.resolve(timelineFixture(["2016-11-04", "2026-10-14", "2026-10-15"]));
  await loading;
  assert.equal(ui.state.dataset, current);
  assert.equal(typeof ui.state.pendingHistory, "function");
  ui.state.requestId += 1;
  ui.state.pendingHistory();
  assert.equal(ui.state.dataset, current, "an older request cannot overwrite a newer view");
});

test("failed background history preserves the working recent data and provides a retry", async () => {
  const ui = harness(async () => ({ httpError: 503, message: "history temporarily unavailable" }));
  ui.state.progressive = true;
  ui.installDataset(timelineFixture(["2026-10-14", "2026-10-15"]));
  const original = ui.state.dataset;
  await ui.loadHistory();
  assert.equal(ui.state.dataset, original);
  assert.equal(ui.state.historyLoading, false);
  assert.match(ui.state.historyError, /history temporarily unavailable/);
  assert.equal(ui.get("historyRetry").hidden, false);
});

test("quiet refresh keeps an old cursor date and cannot overwrite a selection changed in flight", async () => {
  const response = deferred();
  const dates = ["2016-11-04", "2026-10-14", "2026-10-16"];
  const ui = harness(async () => response.promise);
  ui.state.progressive = true; ui.state.historyReady = true; ui.state.mode = "latest";
  ui.installDataset(timelineFixture(dates));
  ui.selectTradingDate("2016-11-04");
  const loading = ui.loadSnapshot({ quiet: true });
  assert.match(ui.requests[0].url, /scope=all/);
  ui.selectTradingDate("2026-10-14");
  response.resolve(timelineFixture(dates));
  await loading;
  assert.equal(ui.state.selectedDate, "2026-10-14");
  assert.equal(ui.state.historyReady, true);
});

test("quiet refresh waits for an active drag to finish before installing data", async () => {
  const response = deferred();
  const dates = ["2026-10-14", "2026-10-15", "2026-10-16"];
  const ui = harness(async () => response.promise);
  ui.installDataset(timelineFixture(dates)); ui.state.mode = "latest";
  const original = ui.state.dataset;
  ui.state.dragging = true;
  const loading = ui.loadSnapshot({ quiet: true });
  response.resolve(timelineFixture(dates));
  await new Promise(setImmediate);
  assert.equal(ui.state.dataset, original);
  assert.equal(ui.state.dragWaiters.length, 1);
  ui.state.dragging = false;
  ui.state.dragWaiters.splice(0).forEach((resolve) => resolve());
  await loading;
  assert.notEqual(ui.state.dataset, original);
});
