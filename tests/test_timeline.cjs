"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { prepare, project } = require("../national_team_radar/static/timeline.js");

test("columnar transport is lossless, with historical nulls and no borrowed ETF or benchmark rows", () => {
  const data = {
    as_of: "2026-10-14", summary: { date: "2026-10-14", flow_yi: 7 },
    series: { columns: ["date", "flow_yi", "complete", "valid_count", "total_count"], rows: [["2026-10-12", null, false, 1, 2], ["2026-10-14", 7, true, 2, 2]] },
    etfs: [{ ts_code: "A", name: "A", flow_yi: 3, nav: 999 }, { ts_code: "B", name: "B", flow_yi: 4, nav: 999 }],
    etf_series: { A: { columns: ["date", "flow_yi", "valid", "quarantined"], rows: [["2026-10-12", null, false, true], ["2026-10-14", 3, true, false]] }, B: [{ date: "2026-10-14", flow_yi: 4, valid: true }] },
    metadata: { history: { first_z_date: "2026-10-14" }, benchmark: { columns: ["trade_date", "close"], rows: [["2026-10-14", 4000]] } },
    quality: { warnings: ["latest-day-only warning"] },
    evidence: [{ date: "2026-10-12", title: "past" }, { date: "2026-10-14", title: "future" }],
    crosschecks: { channels: { holders: { state: "partial", errors: ["source failed"], rows: [{ date: "2026-10-14" }, { date: "2026-10-12" }] } } },
  };
  const prepared = prepare(data);
  assert.equal(prepared.snapshot.series[0].flow_yi, null);
  const selected = project(prepared.snapshot, prepared.timeline, "2026-10-12");
  assert.equal(selected.summary.flow_yi, null);
  assert.equal(selected.benchmark, null);
  assert.equal(selected.etfs[1].flow_yi, null);
  assert.equal(selected.etfs[1].nav, undefined);
  assert.equal(selected.etfs[0].nav, undefined, "latest ETF fields must never leak into a historical row");
  assert.deepEqual(selected.quality.coverage.missing_codes, ["A", "B"]);
  assert.deepEqual(selected.quality.coverage.quarantined_codes, ["A"]);
  assert.equal(selected.metadata.history.first_z_date, null);
  assert.equal(selected.last_complete, null);
  assert.equal(selected.metadata.benchmark.length, 0);
  assert.equal(selected.evidence.length, 1);
  assert.equal(selected.crosschecks.channels.holders.total_records, 1);
  assert.deepEqual(selected.crosschecks.channels.holders.errors, ["source failed"]);
  assert.equal(selected.crosschecks.channels.holders.state, "partial");
  assert.equal(data.etf_series.B.length, 1, "source histories are never truncated by selection");
  assert.equal(project(prepared.snapshot, prepared.timeline, "2026-10-13"), null);
});

test("the worker fetches, decodes and indexes data, and reports HTTP failures without publishing a result", async () => {
  const sent = [];
  const sandbox = {
    self: { postMessage(message) { sent.push(message); } },
    importScripts() {},
    RadarTimeline: { prepare },
    async fetch(url) { return url === "/bad" ? { ok: false, status: 503, json: async () => ({ detail: "not available" }) } : { ok: true, json: async () => ({ as_of: "2026-10-14", series: [{ date: "2026-10-14" }] }) }; },
  };
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(path.join(__dirname, "../national_team_radar/static/data-worker.js"), "utf8"), sandbox);
  await sandbox.self.onmessage({ data: { id: 1, url: "/good" } });
  assert.equal(sent[0].result.timeline.index.get("2026-10-14"), 0);
  await sandbox.self.onmessage({ data: { id: 2, url: "/bad" } });
  assert.equal(sent[1].error, "not available");
  assert.equal(sent[1].result, undefined);
});
