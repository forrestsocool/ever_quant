"use strict";

// Shared by the data worker and the controller's worker-free fallback.
// Date indexes are prepared once, never rebuilt during pointer movement.
((root) => {
  const rows = (value) => Array.isArray(value) ? value : value?.rows?.map((cells) => Object.fromEntries(value.columns.map((key, i) => [key, cells[i]]))) ?? [];
  const day = (row) => String(row?.date ?? row?.trade_date ?? "").slice(0, 10);
  const upperBound = (values, date, key = day) => {
    let lo = 0; let hi = values.length;
    while (lo < hi) { const mid = (lo + hi) >>> 1; if (key(values[mid]) <= date) lo = mid + 1; else hi = mid; }
    return lo;
  };
  function prepare(input) {
    const snapshot = input.snapshot ?? input;
    snapshot.series = rows(snapshot.series);
    snapshot.etf_series = Object.fromEntries(Object.entries(snapshot.etf_series ?? {}).map(([code, value]) => [code, rows(value)]));
    snapshot.metadata = { ...snapshot.metadata, benchmark: rows(snapshot.metadata?.benchmark) };
    const index = new Map(snapshot.series.map((row, i) => [day(row), i]));
    const etfs = new Map(Object.entries(snapshot.etf_series).map(([code, entries]) => [code, new Map(entries.map((row) => [day(row), row]))]));
    const benchmark = new Map(snapshot.metadata.benchmark.map((row) => [day(row), row]));
    let last = null; let complete = 0;
    const lastComplete = []; const completeDays = [];
    snapshot.series.forEach((row) => {
      if (row.complete) { last = row; complete += 1; }
      lastComplete.push(last); completeDays.push(complete);
    });
    const crosschecks = Object.fromEntries(Object.entries(snapshot.crosschecks?.channels ?? {}).map(([key, value]) => [key, [...(value.rows ?? [])].sort((a, b) => day(a).localeCompare(day(b)))]));
    return { snapshot, timeline: { index, etfs, benchmark, lastComplete, completeDays, crosschecks } };
  }
  function project(dataset, timeline, date) {
    const i = timeline.index.get(date);
    if (i === undefined) return null;
    const summary = { ...dataset.series[i], identity_confirmed: false, interpretation: dataset.summary?.interpretation };
    const etfs = (dataset.etfs ?? []).map((etf) => {
      const row = timeline.etfs.get(etf.ts_code)?.get(date);
      const definition = Object.fromEntries(["ts_code", "code", "name", "index", "index_name", "list_date", "found_date"].filter((key) => key in etf).map((key) => [key, etf[key]]));
      return row ? { ...definition, ...row } : { ...definition, date, valid: false, flow_yi: null, status: "数据不足", quality: ["所选交易日记录缺失"] };
    });
    const missing = etfs.filter((etf) => !etf.valid).map((etf) => etf.ts_code);
    const quarantined = etfs.filter((etf) => etf.quarantined).map((etf) => etf.ts_code);
    const history = dataset.metadata?.history ?? {};
    const benchmarkEnd = upperBound(dataset.metadata?.benchmark ?? [], date);
    const quality = {
      ...dataset.quality, warnings: [...(summary.quality ?? [])],
      coverage: { valid_count: summary.valid_count, total_count: summary.total_count, missing_codes: missing, quarantined_codes: quarantined, complete_days: timeline.completeDays[i], total_days: i + 1 },
      latest_share_date: date,
      latest_quote_date: etfs.some((etf) => etf.close !== null && etf.close !== undefined) ? date : null,
      calendar_latest_session: date, pending_dates: [], quote_pending_dates: [],
      issue_counts: {},
    };
    return {
      ...dataset, as_of: date, summary, etfs, quality,
      series: dataset.series.slice(0, i + 1), last_complete: timeline.lastComplete[i],
      benchmark: timeline.benchmark.get(date) ?? null,
      alerts: (dataset.alerts ?? []).filter((row) => day(row) <= date),
      observation_events: (dataset.observation_events ?? []).filter((row) => day(row) <= date),
      evidence: (dataset.evidence ?? []).filter((row) => day(row) <= date),
      metadata: { ...dataset.metadata, view_end_date: date, expected_latest_trade_date: date, benchmark: dataset.metadata.benchmark.slice(0, benchmarkEnd),
        history: { ...history, requested_end: date, trading_days: i + 1, complete_days: timeline.completeDays[i],
          first_z_date: history.first_z_date && history.first_z_date <= date ? history.first_z_date : null } },
      crosschecks: { ...dataset.crosschecks, channels: Object.fromEntries(Object.entries(dataset.crosschecks?.channels ?? {}).map(([key, channel]) => {
        const entries = timeline.crosschecks[key];
        const end = upperBound(entries, date);
        return [key, { ...channel, total_records: end, returned: end, rows: entries.slice(Math.max(0, end - 20), end).reverse(), review_cutoff: date }];
      })) },
      selection: { date, latest: dataset.as_of, historical: date !== dataset.as_of, policy: "按当前披露数据版本回看；不重建当时可见数据。" },
    };
  }
  root.RadarTimeline = { prepare, project, upperBound };
  if (typeof module !== "undefined") module.exports = root.RadarTimeline;
})(globalThis);
