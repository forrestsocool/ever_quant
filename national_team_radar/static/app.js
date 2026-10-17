"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const DEFAULTS = { window: 20, z_threshold: 2, min_flow_yi: 10, sync_min: 3 };
  const state = {
    snapshot: null, config: {}, params: { ...DEFAULTS }, range: "252", indexVisible: true,
    search: "", index: "all", sort: "flow_yi", sortDirection: -1,
    refreshing: false, polling: null, requestId: 0, selectedEtf: null,
    drawerMode: "flow", alertsExpanded: false, loaded: false, previousFocus: null,
    mode: "latest", latestSnapshot: null, baseStart: null, loading: false,
    dateEditorOpen: false, dateDraftDirty: false, statusInitialized: false, lastSeenUpdate: null,
    pendingServerUpdate: false, lastStatusError: null, newDataAt: null, statusBusy: false, refreshEpoch: 0,
    dataset: null, timeline: null, selectedDate: null, restoreDate: null, progressive: false,
    historyLoading: false, historyReady: false, historyError: null, historyEpoch: 0,
    dragging: false, cursorFrame: null, chartController: null, pendingHistory: null,
    dragWaiters: [],
  };
  const preparedSnapshots = new WeakMap();
  const numberFormats = new Map();
  const updatedFormat = new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
  const finite = (value) => value !== null && value !== undefined && value !== "" && Number.isFinite(Number(value));
  const num = (value) => finite(value) ? Number(value) : null;
  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
  const number = (value, decimals = 2, sign = false) => {
    if (!finite(value)) return "—";
    const n = Number(value);
    if (!numberFormats.has(decimals)) numberFormats.set(decimals, new Intl.NumberFormat("en-US", { minimumFractionDigits: decimals, maximumFractionDigits: decimals }));
    return `${sign && n > 0 ? "+" : ""}${numberFormats.get(decimals).format(n)}`;
  };
  const colorClass = (value) => !finite(value) || Number(value) === 0 ? "neutral-text" : Number(value) > 0 ? "positive-text" : "negative-text";
  const codeOf = (etf) => String(etf.ts_code ?? etf.code ?? "");
  const dateString = (value) => {
    if (!value) return "";
    const text = String(value).slice(0, 10);
    if (/^\d{8}$/.test(text)) return `${text.slice(0, 4)}-${text.slice(4, 6)}-${text.slice(6, 8)}`;
    return /^\d{4}-\d{2}-\d{2}$/.test(text) ? text : "";
  };
  const shortDate = (value) => dateString(value).slice(5) || "—";
  const percent = (value) => `${number(value, 2, true)}${finite(value) ? "%" : ""}`;
  const safeUrl = (value) => {
    try { const url = new URL(String(value)); return ["https:", "http:"].includes(url.protocol) ? url.href : ""; } catch { return ""; }
  };
  const array = (value) => Array.isArray(value) ? value : [];
  const chartEmpty = (title, detail = "") => `<div class="chart-empty"><div class="empty-orbit"></div><strong>${esc(title)}</strong><p>${esc(detail)}</p></div>`;

  async function api(path, options = {}) {
    let response;
    try { response = await fetch(path, { ...options, headers: { "Accept": "application/json", ...(options.body ? { "Content-Type": "application/json" } : {}), ...options.headers } }); }
    catch { throw new Error("无法连接本地数据服务。请确认雷达服务正在运行，然后重试。"); }
    let data;
    try { data = await response.json(); } catch { throw new Error(`本地服务返回了无法识别的数据（HTTP ${response.status}）。`); }
    if (!response.ok) {
      const detail = data.detail ?? data.message ?? data.error;
      throw new Error(typeof detail === "string" ? detail : `请求失败（HTTP ${response.status}），请检查数据服务。`);
    }
    return data;
  }

  let dataWorker = null; let workerSequence = 0;
  const workerRequests = new Map();
  function snapshotRequest(params, scope = "year") {
    if (!state.progressive) return api(`/api/snapshot?${queryParams(params)}`);
    const url = `/api/dashboard?${queryParams({ ...params, scope })}`;
    const fallback = () => api(url).then((data) => {
      const prepared = RadarTimeline.prepare(data);
      preparedSnapshots.set(prepared.snapshot, prepared);
      return prepared.snapshot;
    });
    if (typeof Worker !== "undefined") {
      try {
      if (!dataWorker) {
        dataWorker = new Worker("/static/data-worker.js?v=20261017-scrub");
        const worker = dataWorker;
        dataWorker.onmessage = ({ data }) => {
          const request = workerRequests.get(data.id);
          if (!request) return;
          workerRequests.delete(data.id);
          if (data.error) request.reject(new Error(data.error));
          else { preparedSnapshots.set(data.result.snapshot, data.result); request.resolve(data.result.snapshot); }
        };
        dataWorker.onerror = () => {
          if (dataWorker !== worker) return;
          const failed = [...workerRequests.values()];
          workerRequests.clear(); worker.terminate(); dataWorker = null;
          for (const request of failed) request.fallback().then(request.resolve, request.reject);
        };
      }
      return new Promise((resolve, reject) => { const id = ++workerSequence; workerRequests.set(id, { resolve, reject, fallback }); dataWorker.postMessage({ id, url }); });
      } catch { /* Restricted browsers can still load and use the dashboard. */ }
    }
    return fallback();
  }

  function installDataset(snapshot, date = null) {
    const prepared = preparedSnapshots.get(snapshot) ?? RadarTimeline.prepare(snapshot);
    state.dataset = prepared.snapshot; state.timeline = prepared.timeline;
    const wanted = state.restoreDate ?? date;
    state.selectedDate = state.timeline.index.has(wanted) ? wanted : snapshot.as_of;
    if (state.timeline.index.has(state.restoreDate)) state.restoreDate = null;
    state.snapshot = selectedSnapshot(state.selectedDate) ?? snapshot;
  }

  function selectedSnapshot(date) {
    return date === state.dataset.as_of ? { ...state.dataset, selection: { date, latest: date, historical: false } } : RadarTimeline.project(state.dataset, state.timeline, date);
  }

  function renderHistoryStatus() {
    const node = $("historyLoadStatus");
    if (!node) return;
    node.textContent = state.historyLoading ? "完整历史后台加载中…" : state.historyError ? "完整历史加载失败，当前数据仍可使用" : state.historyReady ? "完整历史已缓存 · 拖动无需联网" : "近一年已就绪";
    node.className = `history-load-status${state.historyError ? " error" : ""}`;
    $("historyRetry").hidden = !state.historyError;
  }

  async function loadHistory(requestId = state.requestId) {
    const epoch = ++state.historyEpoch;
    state.historyLoading = true; state.historyError = null; renderHistoryStatus();
    try {
      const params = { ...state.params, start_date: [state.params.start_date, state.config.history_start_date ?? state.baseStart].filter(Boolean).sort()[0] };
      const result = await snapshotRequest(params, "all");
      if (requestId !== state.requestId || epoch !== state.historyEpoch) return;
      const commit = () => {
        if (requestId !== state.requestId || epoch !== state.historyEpoch) return;
        installDataset(result.snapshot ?? result, state.snapshot?.selection?.historical ? state.selectedDate : null);
        state.historyReady = true; state.historyLoading = false; state.pendingHistory = null;
        renderAll(); renderViewChrome(); renderHistoryStatus(); setExportUrl();
      };
      // Do not replace the SVG while it owns pointer capture.
      if (state.dragging) state.pendingHistory = commit;
      else commit();
    } catch (error) {
      if (requestId !== state.requestId || epoch !== state.historyEpoch) return;
      state.historyLoading = false; state.historyError = error.message; renderHistoryStatus();
    }
  }

  function selectTradingDate(date, { persist = false } = {}) {
    if (!state.timeline?.index.has(date)) return false;
    if (!state.dragging) $("chartTooltip").hidden = true;
    state.restoreDate = null;
    if (state.selectedDate !== date) {
      state.selectedDate = date;
      state.snapshot = selectedSnapshot(date);
      renderOverview(); renderTable(); renderAlerts(); renderEvidence(true);
      if (state.selectedEtf) renderDrawer();
      renderViewChrome(); setExportUrl();
      presentDataWarnings();
    }
    state.chartController?.select(date);
    renderDateSelection();
    if (persist) persistView();
    return true;
  }

  function renderDateSelection() {
    const date = state.selectedDate ?? state.snapshot?.as_of;
    $("selectedTradeDate").textContent = date || "—";
    const series = state.chartController?.series ?? [];
    const i = state.chartController?.dateIndex.get(date) ?? -1;
    $("previousTradeDate").disabled = i <= 0;
    $("nextTradeDate").disabled = i < 0 || i >= series.length - 1;
    $("returnLatestDate").disabled = !state.dataset || date === state.dataset.as_of;
    $("selectionContext").textContent = date && date !== state.dataset?.as_of ? "所选日联动 · 按当前披露版本回看" : "最新交易日 · 全页数据联动";
  }

  function notify(message, kind = "info") {
    $("notice").className = `notice ${kind}`;
    $("noticeText").textContent = message;
    $("noticeSymbol").textContent = kind === "error" ? "!" : kind === "success" ? "✓" : "i";
    $("notice").hidden = !message;
  }

  function queryParams(params = state.params) {
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(params)) if (value !== null && value !== undefined && value !== "") query.set(key, String(value));
    return query;
  }

  function setExportUrl() {
    const link = $("exportButton");
    const params = state.snapshot?.selection?.historical ? { ...state.params, review_date: state.snapshot.as_of } : state.params;
    link.href = `/api/export.csv?${queryParams(params)}`;
    link.setAttribute("aria-disabled", state.snapshot ? "false" : "true");
    link.title = state.snapshot ? `导出 ${params.start_date} 至 ${params.review_date ?? params.end_date}，使用当前监测参数及披露数据版本` : "载入真实数据后可导出";
  }

  function readDateDraft() {
    const start = $("startDate").value;
    const end = $("endDate").value;
    if (!start || !end) { notify("请选择完整的开始日期和结束日期。", "error"); return null; }
    if (start > end) { notify("开始日期需要早于或等于结束日期。", "error"); return null; }
    return { ...state.params, start_date: start, end_date: end };
  }

  function syncDateInputs(snapshot = {}) {
    const metadata = snapshot.metadata ?? {};
    const config = snapshot.config ?? {};
    const defaults = state.config.defaults ?? state.config;
    const start = state.params.start_date ?? metadata.requested_start_date ?? config.start_date ?? defaults.start_date ?? state.config.start_date;
    const end = state.params.end_date ?? metadata.requested_end_date ?? config.end_date ?? defaults.end_date ?? state.config.end_date ?? state.config.today ?? state.config.client_date;
    if (dateString(start)) { $("startDate").value = dateString(start); state.params.start_date = dateString(start); }
    if (dateString(end)) { $("endDate").value = dateString(end); state.params.end_date = dateString(end); }
    setExportUrl();
  }

  function latestEnd() {
    return dateString(state.config.today ?? state.config.client_date ?? state.config.end_date) || dateString(state.latestSnapshot?.metadata?.expected_latest_trade_date) || state.params.end_date;
  }

  function latestParams(params = state.params) {
    return { ...params, start_date: state.config.history_start_date ?? state.baseStart ?? state.config.start_date ?? params.start_date, end_date: latestEnd() };
  }

  function readUrlState() {
    const query = new URLSearchParams(window.location.search);
    const params = {};
    const limits = { window: [5, 120], z_threshold: [.5, 10], min_flow_yi: [.1, 10000], sync_min: [1, 7] };
    for (const [key, [min, max]] of Object.entries(limits)) {
      const value = Number(query.get(key));
      if (query.has(key) && Number.isFinite(value) && value >= min && value <= max && (!["window", "sync_min"].includes(key) || Number.isInteger(value))) params[key] = value;
    }
    for (const key of ["start_date", "end_date"]) if (dateString(query.get(key))) params[key] = dateString(query.get(key));
    const mode = ["complete", "latest", "custom"].includes(query.get("view")) ? query.get("view") : "latest";
    const range = ["20", "60", "120", "252", "1260", "all"].includes(query.get("range")) ? query.get("range") : "252";
    return { params, mode, range, date: dateString(query.get("date")), indexVisible: query.get("index") !== "off" };
  }

  function persistView() {
    const query = new URLSearchParams();
    query.set("view", state.mode); query.set("range", state.range);
    query.set("index", state.indexVisible ? "on" : "off");
    for (const key of Object.keys(DEFAULTS)) query.set(key, state.params[key]);
    if (state.params.start_date) query.set("start_date", state.params.start_date);
    if (state.mode === "custom" && state.params.end_date) query.set("end_date", state.params.end_date);
    if (state.snapshot?.selection?.historical) query.set("date", state.selectedDate);
    window.history.replaceState(null, "", `${window.location.pathname}?${query}${window.location.hash}`);
  }

  function renderViewChrome() {
    const data = state.snapshot;
    const asOf = dateString(data?.as_of);
    const historical = data?.selection?.historical;
    const label = historical ? "所选交易日" : state.mode === "complete" ? "最近完整交易日" : state.mode === "latest" ? "最新披露交易日" : "历史观察交易日";
    $("asOfLabel").textContent = label;
    $("heroDateLabel").textContent = `${asOf || "—"} · 宽基组合${historical ? " · 历史回看" : ""}`;
    $("viewContext").innerHTML = state.loading ? `<span class="spinner"></span><span>正在读取${state.snapshot ? `，仍展示 ${esc(asOf)}` : "真实数据…"}</span>` : data ? `<span class="view-mode-label ${state.mode === "custom" ? "historical" : ""}">${esc(state.mode === "complete" ? "完整数据" : state.mode === "latest" ? "披露进度" : "历史视图")}</span><span>${esc(state.params.start_date)} 至 ${esc(state.params.end_date)}</span>${state.mode === "custom" ? '<button class="text-button" id="returnComplete">返回最近完整日 ↗</button>' : ""}` : "尚未载入真实数据";
    $("returnComplete")?.addEventListener("click", () => loadSnapshot({ mode: "complete" }));
    $("customDateEditor").hidden = !state.dateEditorOpen && state.mode !== "custom";
    $("viewModeControls").querySelectorAll("button").forEach((button) => {
      const active = button.dataset.view === state.mode;
      button.classList.toggle("active", active); button.setAttribute("aria-pressed", active ? "true" : "false"); button.disabled = state.loading;
      if (button.dataset.view === "custom") button.setAttribute("aria-expanded", $("customDateEditor").hidden ? "false" : "true");
    });
    const latest = state.latestSnapshot;
    if (latest) {
      const summary = latest.summary ?? {};
      const valid = summary.valid_count ?? 0; const total = summary.total_count ?? 7;
      const complete = finite(summary.flow_yi) && valid === total;
      $("latestProgress").className = `latest-progress ${complete ? "complete" : "pending"}`;
      $("latestProgress").innerHTML = `<span class="mini-dot"></span><span>最新披露 <strong>${esc(dateString(latest.as_of))}</strong></span><span class="disclosure-count">${valid} / ${total} 只有效</span><span class="disclosure-copy">${complete ? "已完整" : "组合待齐"}</span>${state.newDataAt && state.mode === "custom" ? '<span class="background-updated">后台已更新</span>' : ""}${state.mode !== "latest" ? '<button class="text-button" id="openLatestDisclosure">查看 ↗</button>' : ""}`;
      $("openLatestDisclosure")?.addEventListener("click", () => loadSnapshot({ mode: "latest" }));
      if (state.newDataAt) $("latestProgress").title = `已检测到后台更新：${state.newDataAt}；历史视图保持所选日期。`;
    }
  }

  function presentDataWarnings() {
    const meta = state.snapshot?.metadata ?? {};
    const warnings = array(state.snapshot?.quality?.warnings).filter(Boolean).filter((warning) => !/^已排除\s*\d+\s*条非交易日/.test(String(warning)));
    const errors = array(meta.errors).filter(Boolean);
    if (state.lastStatusError) notify(`最近一次更新未完成：${state.lastStatusError}。当前保留 ${state.snapshot?.as_of ?? "原有"} 视图。`, "error");
    else if (meta.is_stale) notify(`当前展示缓存数据。${errors[0] ?? "数据源更新未完成，点击更新可重试。"}`);
    else if (errors.length) notify(errors.slice(0, 2).join("；"));
    else if (warnings.length) notify(warnings.slice(0, 2).join("；"));
    else $("notice").hidden = true;
  }

  async function loadSnapshot({ quiet = false, mode = state.mode, params = state.params } = {}) {
    const requestId = ++state.requestId;
    state.historyEpoch += 1; state.pendingHistory = null;
    const fullRefresh = quiet && state.historyReady && mode !== "custom";
    const readScope = fullRefresh ? "all" : "year";
    if (!quiet && state.dragging) state.chartController?.dispose();
    const candidate = { ...params };
    let draft = null;
    state.loading = true;
    $("dateApply").disabled = true;
    if (!quiet) $("connectionLabel").textContent = "正在读取数据";
    renderViewChrome();
    try {
      let selected; let newest = state.latestSnapshot; let displayParams = candidate;
      if (mode === "custom") {
        const result = await snapshotRequest(candidate, "custom");
        selected = result.snapshot ?? result;
        // Disclosure progress has its own date and never borrows the historical view.
        if (!newest) {
          try { const latest = await snapshotRequest(latestParams(candidate), readScope); newest = latest.snapshot ?? latest; } catch { /* Historical data remains useful if latest coverage is unavailable. */ }
        }
      } else {
        const newestQuery = latestParams(candidate);
        const result = await snapshotRequest(newestQuery, readScope);
        newest = result.snapshot ?? result;
        displayParams = { ...newestQuery };
        selected = newest;
        // A historical cutoff can hide a NAV published the following day.
        // Recheck completeness after that cutoff rather than retaining its label.
        for (let attempt = 0; mode === "complete" && !selected.summary?.complete && attempt < 8; attempt += 1) {
          const completeDate = dateString(selected.last_complete?.date);
          if (!completeDate || completeDate >= dateString(selected.as_of)) break;
          displayParams.end_date = completeDate;
          if (displayParams.start_date > completeDate) displayParams.start_date = completeDate;
          const complete = await snapshotRequest(displayParams, readScope);
          selected = complete.snapshot ?? complete;
        }
        if (mode === "complete" && !selected.summary?.complete) throw new Error("当前无法形成完整组合，请切换到最新披露查看数据缺口。");
      }
      if (quiet && state.dragging) await new Promise((resolve) => state.dragWaiters.push(resolve));
      if (requestId !== state.requestId) return;
      if (quiet) draft = { start: $("startDate").value, end: $("endDate").value, editorOpen: state.dateEditorOpen, dirty: state.dateDraftDirty };
      installDataset(selected, quiet && state.snapshot?.selection?.historical ? state.selectedDate : null);
      state.historyReady = fullRefresh || !state.progressive || mode === "custom";
      state.historyLoading = false; state.historyError = null;
      state.latestSnapshot = newest;
      state.mode = mode;
      state.params = { ...displayParams };
      if (mode === "custom") state.baseStart = displayParams.start_date;
      state.dateEditorOpen = draft ? draft.editorOpen : mode === "custom";
      state.dateDraftDirty = Boolean(draft?.dirty);
      state.loaded = true;
      state.loading = false;
      syncDateInputs(state.snapshot);
      if (draft?.dirty) { $("startDate").value = draft.start; $("endDate").value = draft.end; }
      renderAll(); renderViewChrome(); persistView();
      renderHistoryStatus();
      $("dateDraftNote").textContent = draft?.dirty ? "日期尚未应用；当前图表和导出保持原区间。" : "图表、基金明细与导出使用同一已展示区间。";
      $("connectionDot").className = "connection-dot connected";
      $("connectionLabel").textContent = "Tushare · 本地数据";
      presentDataWarnings();
      if (state.progressive && !fullRefresh) {
        // Paint the usable recent view before requesting the full history.
        const background = () => { if (requestId === state.requestId) loadHistory(requestId); };
        if (typeof requestIdleCallback === "function") requestIdleCallback(background, { timeout: 800 });
        else setTimeout(background, 0);
      }
      return true;
    } catch (error) {
      if (requestId !== state.requestId) return;
      state.loading = false;
      if (!quiet) state.dateDraftDirty = false;
      $("connectionDot").className = "connection-dot error";
      $("connectionLabel").textContent = "数据暂不可用";
      if (!quiet) syncDateInputs();
      renderViewChrome();
      notify(`${error.message}${state.snapshot ? ` 当前仍展示 ${state.snapshot.as_of} 的原有数据，日期与导出均已保留。` : ""}`, "error");
      if (!state.snapshot) $("mainChart").innerHTML = chartEmpty("尚未取得监测数据", "点击「更新真实数据」，使用本地 Tushare 认证获取数据");
      return false;
    } finally {
      if (requestId === state.requestId) $("dateApply").disabled = false;
    }
  }

  function setRefreshing(running, stage = "") {
    state.refreshing = running;
    $("refreshButton").disabled = running;
    $("refreshButton").querySelector("span").textContent = running ? "正在更新" : "更新真实数据";
    $("refreshButton").querySelector("svg").style.animation = running ? "spin 1.5s linear infinite" : "";
    $("refreshProgress").hidden = !running;
    $("refreshStage").textContent = stage || "正在使用本地 Tushare 认证更新数据…";
  }

  async function refreshData() {
    if (state.refreshing) return;
    state.refreshEpoch += 1;
    setRefreshing(true, "正在准备更新观察池…");
    $("notice").hidden = true;
    try {
      // Download the latest range without committing an un-applied date draft.
      const result = await api("/api/refresh", { method: "POST", body: JSON.stringify({ start_date: state.baseStart ?? state.params.start_date ?? null, end_date: latestEnd() ?? null }) });
      if (result.error) throw new Error(String(result.error));
      setRefreshing(true, result.stage);
      pollStatus();
    } catch (error) { setRefreshing(false); notify(error.message, "error"); }
  }

  async function pollStatus() {
    if (state.statusBusy) return;
    state.statusBusy = true;
    const refreshEpoch = state.refreshEpoch;
    clearTimeout(state.polling);
    try {
      const status = await api("/api/status");
      if (refreshEpoch !== state.refreshEpoch) return;
      const updateSignature = JSON.stringify([status.completed_at, status.last_attempt_at, status.updated_at, status.stage, Boolean(status.degraded)]);
      const changed = state.statusInitialized && updateSignature !== state.lastSeenUpdate;
      const wasRefreshing = state.refreshing;
      state.statusInitialized = true;
      state.lastSeenUpdate = updateSignature;
      state.lastStatusError = status.error ? String(status.error) : null;
      if (status.running) {
        setRefreshing(true, typeof status.stage === "string" ? status.stage : "正在更新 ETF 份额、净值与行情…");
      } else {
        setRefreshing(false);
        if (changed || wasRefreshing) state.pendingServerUpdate = true;
        if (state.pendingServerUpdate && !state.loading) {
          state.pendingServerUpdate = false;
          const viewRequestId = state.requestId;
          const mode = state.mode; const params = { ...state.params };
          state.newDataAt = status.completed_at ?? status.updated_at;
          try { state.config = await api("/api/config"); } catch { /* Existing config still describes the current view. */ }
          if (state.requestId !== viewRequestId || state.loading) { state.pendingServerUpdate = true; return; }
          // Refresh current mode; a custom historical end date is never replaced.
          if (mode === "custom") {
            try {
              const latest = await snapshotRequest(latestParams(params));
              if (state.requestId === viewRequestId && !state.loading) state.latestSnapshot = latest.snapshot ?? latest;
            } catch { /* Keep last known disclosure progress. */ }
          }
          if (state.requestId !== viewRequestId || state.loading) { state.pendingServerUpdate = true; return; }
          await loadSnapshot({ quiet: true, mode, params });
        }
        if (status.error) notify(`最近一次更新未完成：${status.error}。${state.snapshot ? `当前仍展示 ${state.snapshot.as_of} 的已有数据。` : ""}`, "error");
        else if (status.degraded && status.warning) notify(status.warning);
      }
    } catch (error) { setRefreshing(false); notify(`后台更新状态暂不可用：${error.message} 当前页面保留已载入数据。`, "error"); }
    finally { state.statusBusy = false; state.polling = setTimeout(pollStatus, state.refreshing ? 1600 : 30000); }
  }

  function signalInfo(status, values = {}) {
    const text = String(status ?? "");
    const outflow = /净赎回|净流出|outflow|out|sell/i.test(text);
    const inflow = !outflow && /净申购|净流入|inflow|in|buy/i.test(text);
    const unknown = /不足|缺失|等待|unknown|insufficient|pending/i.test(text) || !finite(values.flow_yi);
    if (unknown) return finite(values.flow_yi) ? { kind: "unknown", title: "历史基准不足", badge: "基准不足", short: "基准不足" } : { kind: "unknown", title: "等待完整份额披露", badge: "数据不足", short: "待披露" };
    if (outflow) return { kind: "outflow", title: "大额净赎回异动", badge: "关注资金撤出", short: "净赎回异动" };
    if (inflow) return { kind: "inflow", title: "大额净申购异动", badge: "关注资金需求", short: "净申购异动" };
    return { kind: "normal", title: "资金处于观察区间", badge: "持续观察", short: "观察中" };
  }

  function renderAll() {
    renderOverview();
    renderMainChart();
    renderTableFilters();
    renderTable();
    renderAlerts();
    renderEvidence();
    renderMethod();
    if (state.selectedEtf) renderDrawer();
    renderDateSelection();
  }

  function renderOverview() {
    const data = state.snapshot;
    const summary = data.summary ?? {};
    const meta = data.metadata ?? {};
    const info = signalInfo(summary.status, summary);
    const total = num(summary.total_count) ?? array(data.etfs).length;
    const valid = num(summary.valid_count) ?? 0;
    const missing = Math.max(total - valid, 0);
    const asOf = dateString(data.as_of);
    $("asOf").textContent = asOf || "—";
    const updatedAt = data.refresh?.updated_at ?? meta.updated_at ?? meta.fetched_at ?? data.updated_at;
    $("updatedAt").textContent = updatedAt ? formatUpdatedAt(updatedAt) : asOf ? "以已披露份额为准" : "等待载入真实数据";
    $("heroFlow").textContent = number(summary.flow_yi, 2, true);
    $("heroFlow").className = `hero-value numeric ${finite(summary.flow_yi) ? colorClass(summary.flow_yi) : ""}`;
    $("heroDirection").textContent = !finite(summary.flow_yi) ? (missing ? `等待 ${missing} 只披露` : "等待完整数据") : summary.flow_yi > 0 ? "净申购" : summary.flow_yi < 0 ? "净赎回" : "申赎平衡";
    $("heroDirection").className = `flow-tag ${finite(summary.flow_yi) ? summary.flow_yi > 0 ? "inflow" : summary.flow_yi < 0 ? "outflow" : "" : ""}`;
    $("coverageText").textContent = `${valid} / ${total} 只 ETF 数据有效${missing ? " · 组合金额暂不计算" : " · 观察池已完整覆盖"}`;
    const benchmark = data.benchmark;
    const heroNote = document.querySelector(".hero-note");
    if (benchmark && finite(benchmark.pct_chg)) {
      heroNote.textContent = `沪深300 ${percent(benchmark.pct_chg)}`;
      heroNote.className = `hero-note ${colorClass(benchmark.pct_chg)}`;
      heroNote.title = `沪深300现货指数 · ${dateString(benchmark.trade_date) || asOf} · ${number(benchmark.close, 2)} 点`;
    } else { heroNote.textContent = "份额 × 前日净值"; heroNote.className = "hero-note"; heroNote.title = ""; }
    let reference = $("lastCompleteRef");
    if (!reference) {
      reference = document.createElement("div"); reference.id = "lastCompleteRef"; reference.className = "last-complete-reference";
      $("heroFlow").closest(".hero-value-row").insertAdjacentElement("afterend", reference);
    }
    const last = data.last_complete;
    reference.hidden = finite(summary.flow_yi) || !last || !finite(last.flow_yi) || last.date === data.as_of;
    if (!reference.hidden) {
      reference.innerHTML = `<span>上一完整日 ${esc(shortDate(last.date))}</span><strong class="numeric ${colorClass(last.flow_yi)}">${number(last.flow_yi, 2, true)} <small>亿元</small></strong><button class="text-button" data-reference-date="${esc(dateString(last.date))}">查看 <span>↗</span></button>`;
      reference.querySelector("button").addEventListener("click", () => viewDate(last.date));
    }
    $("signalCard").className = `signal-card ${info.kind === "inflow" || info.kind === "outflow" ? `alert-${info.kind}` : ""}`;
    $("signalTitle").textContent = info.title;
    $("signalBadge").textContent = info.badge;
    $("signalDescription").textContent = info.kind === "unknown" ? (finite(summary.flow_yi) ? `当前份额与净值已覆盖 ${valid} / ${total} 只 ETF；历史有效样本不足或方差为零，暂不计算异常度。保留资金估算，待基准可用后评估异动。` : `截至 ${asOf || "当前"}，${valid} / ${total} 只 ETF 可计算资金。等待剩余份额或前日净值披露后，再评估完整组合信号。`) : info.kind === "inflow" ? "大额净申购、历史异常度与多基金同步性达到当前监测门槛，提示集中资金需求，需结合官方披露验证。" : info.kind === "outflow" ? "大额净赎回、历史异常度与多基金同步性达到当前监测门槛，提示集中赎回压力，需关注后续持续性。" : "当前组合未同时满足全部异动条件。结合连续交易日的资金方向与 ETF 同步性，持续观察大额资金需求。";
    const flowMet = finite(summary.flow_yi) && Math.abs(summary.flow_yi) >= state.params.min_flow_yi;
    const zExtreme = finite(summary.z_score) && Math.abs(summary.z_score) >= state.params.z_threshold;
    const zMet = zExtreme && finite(summary.flow_yi) && summary.flow_yi * summary.z_score > 0;
    const sync = summary.flow_yi >= 0 ? summary.sync_in : summary.sync_out;
    const syncMet = finite(summary.flow_yi) && finite(sync) && sync >= state.params.sync_min;
    if (info.kind === "normal") {
      const unmet = [];
      if (!flowMet) unmet.push(`组合金额绝对值 ${number(Math.abs(summary.flow_yi))} 亿，低于 ${number(state.params.min_flow_yi, 1)} 亿`);
      if (!zMet) unmet.push(zExtreme ? "资金与 Z 值方向未一致" : `|Z| ${number(Math.abs(summary.z_score))}，低于 ${number(state.params.z_threshold, 1)}`);
      if (!syncMet) unmet.push(`同向达标 ${number(sync, 0)} 只，未达到 ${state.params.sync_min} 只`);
      $("signalDescription").textContent = `${unmet.join("；")}。${summary.sync_observation ? `${summary.sync_observation}；` : ""}继续观察后续资金持续性。`;
    }
    $("signalChecks").innerHTML = `<span class="${flowMet ? "met" : ""}"><i></i>规模 ≥ ${number(state.params.min_flow_yi, 0)} 亿</span><span class="${zMet ? "met" : ""}"><i></i>|Z| ≥ ${number(state.params.z_threshold, 1)}</span><span class="${syncMet ? "met" : ""}"><i></i>同步 ≥ ${state.params.sync_min} 只</span>`;
    $("zScore").textContent = number(summary.z_score, 2, true);
    $("zScore").className = `numeric ${colorClass(summary.z_score)}`;
    $("zChip").textContent = zExtreme ? "异常区间" : finite(summary.z_score) ? "常规区间" : "样本待齐";
    $("zCaption").textContent = finite(summary.z_score) ? `前 ${state.params.window} 个交易日（须完整）` : "历史不足或数据缺失；未知不等于 0";
    $("syncCount").textContent = finite(summary.flow_yi) ? number(summary.sync_in, 0) : "—";
    $("syncCaption").textContent = finite(summary.flow_yi) ? `达到异动门槛 · ${number(summary.sync_out, 0)} 只同步净赎回` : "份额披露不齐，暂不评估同步性";
    $("syncCount").closest(".metric-card").title = "同步数量统计分别达到同方向金额与 Z 值门槛的 ETF，不等于所有同方向申赎的 ETF 数。";
    $("breadthDots").innerHTML = Array.from({ length: Math.min(total, 12) }, (_, i) => `<i class="${finite(summary.flow_yi) && i < (summary.sync_in ?? 0) ? "filled" : ""}"></i>`).join("");
    for (const [id, field] of [["flow5d", "flow_5d_yi"], ["flow20d", "flow_20d_yi"]]) {
      $(id).textContent = number(summary[field], 2, true);
      $(id).className = `numeric ${colorClass(summary[field])}`;
    }
    const series = array(data.series);
    $("spark5d").innerHTML = sparkline(series.slice(-5), "flow_yi", "line");
    $("spark20d").innerHTML = sparkline(series.slice(-20), "flow_yi", "line");
    $("flow20Caption").textContent = finite(summary.flow_20d_yi) ? "持续性比单日波动更值得关注" : "区间存在缺失或样本不足，暂不累计";
  }

  function formatUpdatedAt(value) {
    const str = String(value);
    const date = new Date(str);
    if (!Number.isNaN(date.getTime()) && /[TZ+]/.test(str)) return `${updatedFormat.format(date)} 更新`;
    return `${str.replace("T", " ").slice(5, 16)} 更新`;
  }

  function renderMainChart() {
    state.chartController?.dispose(); state.chartController = null;
    let series = array((state.dataset ?? state.snapshot)?.series);
    if (state.mode === "custom") series = series.filter((row) => row.date >= state.params.start_date);
    if (["252", "1260"].includes(state.range) && series.length) {
      const end = new Date(`${series.at(-1).date}T00:00:00Z`);
      const month = end.getUTCMonth(); const years = state.range === "252" ? 1 : 5;
      end.setUTCFullYear(end.getUTCFullYear() - years);
      if (end.getUTCMonth() !== month) end.setUTCDate(0);
      const cutoff = end.toISOString().slice(0, 10);
      series = series.filter((row) => row.date > cutoff);
    } else if (state.range !== "all") series = series.slice(-Number(state.range));
    // Return to the latest date if a shorter range excludes the old cursor.
    if (state.selectedDate && !series.some((row) => row.date === state.selectedDate) && series.length) selectTradingDate(series.at(-1).date);
    $("chartDays").textContent = `${series.length} 个交易日`;
    const history = (state.dataset ?? state.snapshot)?.metadata?.history;
    if ($("historySummary") && history) $("historySummary").textContent = `共同上市起点 ${history.common_listing_date} · 当前区间 ${history.complete_days} / ${history.trading_days} 日资金数据完整 · Z基准就绪 ${history.first_z_date ?? "尚待完整样本"}`;
    const benchmark = new Map(array((state.dataset ?? state.snapshot)?.metadata?.benchmark)
      .filter((row) => dateString(row.trade_date) && finite(row.close) && Number(row.close) > 0)
      .map((row) => [dateString(row.trade_date), row]));
    const indexRows = series.map((row) => benchmark.get(dateString(row.date ?? row.trade_date)) ?? null);
    const indexAvailable = indexRows.some((row) => row !== null);
    const toggle = $("indexToggle");
    toggle.disabled = !indexAvailable;
    toggle.setAttribute("aria-pressed", state.indexVisible ? "true" : "false");
    toggle.setAttribute("aria-label", `${state.indexVisible ? "隐藏" : "显示"}沪深300指数曲线${indexAvailable ? "" : "（当前区间无指数数据）"}`);
    $("indexToggleState").textContent = indexAvailable ? state.indexVisible ? "显示中" : "已关闭" : "暂无数据";
    const showIndex = state.indexVisible && indexAvailable;
    $("mainChart").setAttribute("aria-label", `每日估算净申赎${showIndex ? "、沪深300指数" : ""}及 Z 异常度图表`);
    $("chartFootText").innerHTML = `<i class="foot-dot"></i>${showIndex ? "左轴为估算净申赎（亿元），右轴为沪深300（点）；" : indexAvailable ? "估算净申赎（亿元）；沪深300指数曲线已关闭。" : "估算净申赎（亿元）；当前区间暂无沪深300指数数据。"}底部为 Z 异常度，缺失值留空。`;
    drawChart($("mainChart"), series, { z: true, field: "flow_yi", indexRows: showIndex ? indexRows : null });
    renderDateSelection();
  }

  function niceMax(value) {
    if (!(value > 0)) return 1;
    const magnitude = 10 ** Math.floor(Math.log10(value));
    const factor = value / magnitude;
    return (factor <= 1 ? 1 : factor <= 2 ? 2 : factor <= 2.5 ? 2.5 : factor <= 5 ? 5 : factor <= 7.5 ? 7.5 : 10) * magnitude;
  }

  function drawPulseChart(container, series, indexRows) {
    if (!series.length) { container.innerHTML = chartEmpty("该区间暂无数据", "调整日期区间或更新真实数据后查看"); return; }
    const width = Math.max(container.clientWidth || 800, 280);
    const height = container.clientHeight || 340;
    const compact = width < 480;
    const hasIndex = Array.isArray(indexRows) && indexRows.some((row) => row && finite(row.close));
    const margin = { left: compact ? 43 : 55, right: hasIndex ? (compact ? 51 : 65) : 16, top: 24, bottom: 28 };
    const zHeight = compact ? 50 : 56;
    const gap = 25;
    const plotWidth = width - margin.left - margin.right;
    const plotHeight = height - margin.top - margin.bottom - zHeight - gap;
    const mainBottom = margin.top + plotHeight;
    const zTop = mainBottom + gap;
    const zBottom = zTop + zHeight;
    const step = plotWidth / series.length;
    const x = (i) => margin.left + step * (i + .5);
    const values = series.map((row) => num(row.flow_yi)).filter((value) => value !== null);
    const indexes = hasIndex ? indexRows.map((row) => row && finite(row.close) ? Number(row.close) : null) : [];
    const indexValues = indexes.filter((value) => value !== null);
    if (!values.length && !indexValues.length) {
      container.innerHTML = chartEmpty("当前区间数据尚不完整", "等待基金份额、对应净值或指数数据披露后绘制");
      return;
    }
    const flowMax = niceMax(Math.max(1, ...values.map(Math.abs)) * 1.12);
    const yFlow = (value) => margin.top + (flowMax - value) / (flowMax * 2) * plotHeight;
    const indexLow = indexValues.length ? Math.min(...indexValues) : 0;
    const indexHigh = indexValues.length ? Math.max(...indexValues) : 0;
    const indexPad = Math.max((indexHigh - indexLow) * .08, indexHigh * .002, 1);
    const indexMin = indexLow - indexPad;
    const indexMax = indexHigh + indexPad;
    const yIndex = (value) => margin.top + (indexMax - value) / (indexMax - indexMin) * plotHeight;
    const zValues = series.map((row) => num(row.z_score)).filter((value) => value !== null);
    const zMax = niceMax(Math.max(state.params.z_threshold * 1.6, ...zValues.map(Math.abs), 3));
    const yZ = (value) => zTop + (zMax - value) / (zMax * 2) * zHeight;
    const barWidth = Math.max(.05, Math.min(18, step * .64));
    const tickFlow = (value) => Math.abs(value) >= 1000 ? `${number(value / 1000, 1)}k` : number(value, Math.abs(value) < 5 ? 1 : 0).replace(/\.0$/, "");
    const tickIndex = (value) => number(value, indexMax - indexMin < 8 ? 1 : 0);
    const description = `每日估算净申赎（左轴亿元）${hasIndex ? "、沪深300指数（右轴点位）" : ""}及下方 Z 异常度，${series.length} 个交易日`;
    let svg = `<svg class="chart-svg pulse-svg" viewBox="0 0 ${width} ${height}" role="img" aria-label="${description}"><title>${description}</title>`;
    svg += `<text class="axis-title" x="${margin.left}" y="13">资金 · 亿元</text>`;
    if (hasIndex) svg += `<text class="axis-title index-axis-title" x="${width - margin.right}" y="13" text-anchor="end">沪深300 · 点</text>`;
    for (let j = 0; j <= 4; j++) {
      const yy = margin.top + plotHeight * j / 4;
      const flowValue = flowMax * (1 - j / 2);
      svg += `<line class="${j === 2 ? "zero-line" : "grid-line"}" x1="${margin.left}" y1="${yy}" x2="${width - margin.right}" y2="${yy}"/><text class="axis-label" x="${margin.left - 8}" y="${yy + 4}" text-anchor="end">${tickFlow(flowValue)}</text>`;
      if (hasIndex) svg += `<text class="axis-label index-axis-label" x="${width - margin.right + 8}" y="${yy + 4}">${tickIndex(indexMax - (indexMax - indexMin) * j / 4)}</text>`;
    }
    const base = yFlow(0);
    series.forEach((row, i) => {
      const value = num(row.flow_yi);
      if (value === null) {
        if (step > 4) svg += `<path class="missing-mark" d="M${x(i) - 1.8} ${base - 1.8}l3.6 3.6m-3.6 0l3.6-3.6"/>`;
        return;
      }
      const top = Math.min(yFlow(value), base);
      svg += `<rect class="bar ${value >= 0 ? "inflow" : "outflow"}" x="${x(i) - barWidth / 2}" y="${top}" width="${barWidth}" height="${Math.max(.8, Math.abs(yFlow(value) - base))}" rx="${barWidth > 3 ? 1.5 : .5}"/>`;
    });
    if (hasIndex) {
      let path = ""; let open = false; let lastPoint = null;
      indexes.forEach((value, i) => {
        if (value === null) { open = false; return; }
        path += `${open ? "L" : "M"}${x(i).toFixed(2)},${yIndex(value).toFixed(2)} `;
        lastPoint = { x: x(i), y: yIndex(value) };
        open = true;
      });
      svg += `<path class="index-path" d="${path}"/>`;
      if (lastPoint) svg += `<circle class="index-last-point" cx="${lastPoint.x}" cy="${lastPoint.y}" r="3.2"/>`;
    }
    svg += `<rect class="z-lane-bg" x="${margin.left}" y="${zTop}" width="${plotWidth}" height="${zHeight}" rx="3"/>`;
    svg += `<text class="z-lane-title" x="${margin.left}" y="${zTop - 7}">Z 异常度</text>`;
    for (const threshold of [state.params.z_threshold, 0, -state.params.z_threshold]) {
      const yy = yZ(threshold);
      svg += `<line class="${threshold === 0 ? "z-zero-line" : "threshold-line"}" x1="${margin.left}" y1="${yy}" x2="${width - margin.right}" y2="${yy}"/>`;
      svg += `<text class="z-lane-tick" x="${margin.left - 8}" y="${yy + 3}" text-anchor="end">${threshold > 0 ? "+" : ""}${number(threshold, threshold === 0 ? 0 : 1)}</text>`;
    }
    if (zValues.length) {
      let path = ""; let open = false;
      series.forEach((row, i) => {
        const value = num(row.z_score);
        if (value === null) { open = false; return; }
        path += `${open ? "L" : "M"}${x(i).toFixed(2)},${yZ(value).toFixed(2)} `;
        open = true;
      });
      svg += `<path class="z-path" d="${path}"/>`;
    } else svg += `<text class="z-empty-label" x="${margin.left + 8}" y="${yZ(0) - 6}">历史基准不足，暂不绘制 Z 值</text>`;
    const labelCount = Math.min(series.length, compact ? 4 : 7);
    const labelled = new Set();
    for (let j = 0; j < labelCount; j++) {
      const i = labelCount === 1 ? 0 : Math.round((series.length - 1) * j / (labelCount - 1));
      if (!labelled.has(i)) svg += `<text class="x-label" x="${x(i)}" y="${height - 9}" text-anchor="middle">${esc(shortDate(series[i].date ?? series[i].trade_date))}</text>`;
      labelled.add(i);
    }
    svg += `<g class="pulse-cursor"><line class="cursor-line" x1="0" x2="0" y1="${margin.top}" y2="${zBottom}"/><circle class="cursor-handle" cx="0" cy="${zBottom}" r="5"/></g>`;
    svg += `<rect class="pulse-hit-area" x="${margin.left}" y="${margin.top}" width="${plotWidth}" height="${zBottom - margin.top + 8}"/>`;
    svg += "</svg>";
    container.innerHTML = svg;
    bindPulseCursor(container, series, { width, margin, step, x, hasIndex, indexRows });
  }

  function bindPulseCursor(container, series, { width, margin, step, x, hasIndex, indexRows }) {
    const guide = container.querySelector(".pulse-cursor");
    const dateIndex = new Map(series.map((row, i) => [dateString(row.date ?? row.trade_date), i]));
    let frame = null; let pending = null; let bounds = null; let tooltipIndex = -1;
    let pointerId = null;
    const tooltip = $("chartTooltip");
    const select = (date) => {
      const i = dateIndex.get(date);
      if (i === undefined) return;
      guide.setAttribute("transform", `translate(${x(i)},0)`);
      container.setAttribute("aria-valuenow", i);
      container.setAttribute("aria-valuetext", `${date}，估算净申赎 ${number(series[i].flow_yi)} 亿元`);
    };
    const show = ({ index, clientX, clientY, selecting }) => {
        const row = series[index];
        if (selecting) selectTradingDate(dateString(row.date ?? row.trade_date));
        const title = dateString(row.date ?? row.trade_date) || "日期未知";
        if (tooltipIndex !== index) {
        let content = `<div class="tooltip-date">${esc(title)}</div><div class="tooltip-row"><span>估算净申赎</span><strong class="${colorClass(row.flow_yi)}">${number(row.flow_yi, 2, true)} 亿元</strong></div>`;
        if (hasIndex) {
          const indexRow = indexRows[index];
          content += indexRow && finite(indexRow.close)
            ? `<div class="tooltip-row"><span>沪深300</span><strong>${number(indexRow.close, 2)} 点</strong></div><div class="tooltip-row"><span>指数涨跌</span><strong class="${colorClass(indexRow.pct_chg)}">${percent(indexRow.pct_chg)}</strong></div>`
            : '<div class="tooltip-row"><span>沪深300</span><strong>当日缺失</strong></div>';
        }
        content += `<div class="tooltip-row"><span>Z 异常度</span><strong>${number(row.z_score, 2, true)}</strong></div>`;
        if (finite(row.valid_count)) content += `<div class="tooltip-row"><span>有效 ETF</span><strong>${number(row.valid_count, 0)} / ${state.snapshot?.summary?.total_count ?? "—"}</strong></div>`;
        if (!finite(row.z_score)) content += '<div class="tooltip-small">Z 值未知：历史不足、数据缺失或零方差。</div>';
        tooltip.innerHTML = content; tooltipIndex = index;
        }
        tooltip.hidden = false;
        // A fixed-size tooltip avoids layout reads inside the drag loop.
        tooltip.style.left = `${Math.max(8, Math.min(window.innerWidth - 244, clientX + 15))}px`;
        tooltip.style.top = `${Math.max(8, Math.min(window.innerHeight - 230, clientY - 220))}px`;
    };
    const flush = () => {
      frame = null; state.cursorFrame = null;
      if (pending) { const update = pending; pending = null; show(update); }
    };
    const schedule = (event, selecting) => {
      if (!bounds) bounds = container.getBoundingClientRect();
      const px = ((event.clientX ?? bounds.left) - bounds.left) * width / (bounds.width || width);
      const index = Math.max(0, Math.min(series.length - 1, Math.floor((px - margin.left) / step)));
      pending = { index, clientX: event.clientX ?? bounds.left + x(index), clientY: event.clientY ?? bounds.top, selecting };
      if (frame === null) { frame = requestAnimationFrame(flush); state.cursorFrame = frame; }
    };
    const finish = (event) => {
      if (pointerId === null || (event.pointerId !== undefined && event.pointerId !== pointerId)) return;
      if (frame !== null) { cancelAnimationFrame(frame); flush(); }
      const captured = pointerId; pointerId = null; state.dragging = false;
      if (container.hasPointerCapture?.(captured)) container.releasePointerCapture(captured);
      container.classList.remove("scrubbing"); tooltip.hidden = true; bounds = null;
      persistView();
      state.dragWaiters.splice(0).forEach((resolve) => resolve());
      if (state.pendingHistory) state.pendingHistory();
    };
    const controller = {
      series, dateIndex, select,
      dispose() {
        if (frame !== null) cancelAnimationFrame(frame);
        frame = null; pending = null; state.cursorFrame = null;
        if (pointerId !== null) {
          const captured = pointerId; pointerId = null;
          if (container.hasPointerCapture?.(captured)) container.releasePointerCapture(captured);
        }
        state.dragging = false; tooltip.hidden = true;
        state.dragWaiters.splice(0).forEach((resolve) => resolve());
        for (const name of ["onpointerdown", "onpointermove", "onpointerup", "onpointercancel", "onlostpointercapture", "onpointerleave", "onkeydown"]) container[name] = null;
        container.setAttribute("aria-disabled", "true");
      },
    };
    state.chartController = controller;
    container.setAttribute("role", "slider"); container.setAttribute("tabindex", "0");
    container.setAttribute("aria-disabled", "false");
    container.setAttribute("aria-label", "交易日选择，拖动竖线或用左右方向键联动全页数据");
    container.setAttribute("aria-valuemin", "0"); container.setAttribute("aria-valuemax", series.length - 1);
    select(state.selectedDate ?? dateString(series.at(-1).date ?? series.at(-1).trade_date));
    container.onpointerdown = (event) => {
      if (event.button !== undefined && event.button !== 0) return;
      if (pointerId !== null || state.loading) return;
      bounds = container.getBoundingClientRect(); pointerId = event.pointerId ?? 1;
      state.dragging = true; container.classList.add("scrubbing");
      container.setPointerCapture?.(pointerId);
      container.focus({ preventScroll: true }); schedule(event, true);
    };
    container.onpointermove = (event) => {
      if (pointerId !== null && event.pointerId !== undefined && event.pointerId !== pointerId) return;
      if (event.pointerType === "touch" && pointerId === null) return;
      schedule(event, pointerId !== null);
    };
    container.onpointerup = finish;
    container.onpointercancel = finish;
    container.onlostpointercapture = finish;
    container.onpointerleave = () => {
      if (pointerId !== null) return;
      if (frame !== null) cancelAnimationFrame(frame);
      frame = null; state.cursorFrame = null; pending = null; bounds = null; tooltip.hidden = true;
    };
    container.onkeydown = (event) => {
      const i = dateIndex.get(state.selectedDate) ?? series.length - 1;
      const next = { ArrowLeft: i - 1, ArrowRight: i + 1, Home: 0, End: series.length - 1 }[event.key];
      if (next === undefined) return;
      event.preventDefault();
      selectTradingDate(dateString(series[Math.max(0, Math.min(series.length - 1, next))].date), { persist: true });
    };
  }

  function drawChart(container, series, { z = false, field = "flow_yi", shares = false, indexRows = null } = {}) {
    if (z && container === $("mainChart")) { drawPulseChart(container, series, indexRows); return; }
    if (!series.length) { container.innerHTML = chartEmpty("该区间暂无数据", "调整日期区间或更新真实数据后查看"); return; }
    const width = Math.max(container.clientWidth || 800, 280);
    const height = container.clientHeight || 280;
    const compact = width < 480;
    const margin = { left: compact ? 41 : 48, right: z ? (compact ? 32 : 43) : 15, top: 18, bottom: 35 };
    const plotWidth = width - margin.left - margin.right;
    const plotHeight = height - margin.top - margin.bottom;
    const values = series.map((row) => num(row[field])).filter((v) => v !== null);
    if (!values.length) { container.innerHTML = chartEmpty("当前区间数据尚不完整", "等待基金份额与对应净值披露后绘制资金变化"); return; }
    const maxAbs = niceMax(Math.max(...values.map(Math.abs)) * 1.12);
    let minValue = shares ? Math.min(...values) * .98 : -maxAbs;
    let maxValue = shares ? Math.max(...values) * 1.02 : maxAbs;
    if (minValue === maxValue) { minValue -= 1; maxValue += 1; }
    const y = (value) => margin.top + ((maxValue - value) / (maxValue - minValue)) * plotHeight;
    const step = plotWidth / series.length;
    const x = (i) => margin.left + step * (i + .5);
    const barWidth = Math.max(.05, Math.min(18, step * .64));
    const zs = series.map((row) => num(row.z_score)).filter((v) => v !== null);
    const maxZ = niceMax(Math.max(state.params.z_threshold * 1.7, ...zs.map(Math.abs), 3));
    const zy = (value) => margin.top + (1 - (value + maxZ) / (maxZ * 2)) * plotHeight;
    const fmtTick = (v) => Math.abs(v) >= 1000 ? `${number(v / 1000, Math.abs(v) % 1000 ? 1 : 0)}k` : number(v, Math.abs(v) >= 5 ? 0 : Math.abs(v) < .1 && v !== 0 ? 2 : 1).replace(/\.0$/, "");
    let svg = `<svg class="chart-svg" viewBox="0 0 ${width} ${height}" role="img" aria-label="${shares ? "基金总份额历史" : "每日估算净申赎及历史异常度"}，${series.length} 个交易日"><title>${shares ? "ETF 总份额（亿份）" : "估算净申赎（亿元）及 Z-Score"}</title>`;
    for (let j = 0; j <= 4; j++) {
      const value = minValue + (maxValue - minValue) * j / 4;
      const yy = y(value);
      svg += `<line class="${Math.abs(value) < 1e-9 ? "zero-line" : "grid-line"}" x1="${margin.left}" y1="${yy}" x2="${width - margin.right}" y2="${yy}"/><text class="axis-label" x="${margin.left - 10}" y="${yy + 3}" text-anchor="end">${fmtTick(value)}</text>`;
      if (z) svg += `<text class="axis-label" x="${width - margin.right + 10}" y="${yy + 3}">${fmtTick(-maxZ + maxZ * 2 * j / 4)}</text>`;
    }
    if (z) for (const threshold of [state.params.z_threshold, -state.params.z_threshold]) svg += `<line class="threshold-line" x1="${margin.left}" y1="${zy(threshold)}" x2="${width - margin.right}" y2="${zy(threshold)}"/>`;
    const base = shares ? y(minValue) : y(0);
    if (shares) {
      let path = ""; let open = false;
      series.forEach((row, i) => { if (finite(row[field])) { path += `${open ? "L" : "M"}${x(i).toFixed(2)},${y(row[field]).toFixed(2)} `; open = true; } else open = false; });
      svg += `<path d="${path}" fill="none" stroke="#759c68" stroke-width="2" stroke-linejoin="round"/>`;
    } else series.forEach((row, i) => {
      const value = num(row[field]);
      if (value === null) { if (step > 4) svg += `<path class="missing-mark" d="M${x(i) - 1.8} ${base - 1.8}l3.6 3.6m-3.6 0l3.6-3.6"/>`; return; }
      const top = Math.min(y(value), base);
      svg += `<rect class="bar ${value >= 0 ? "inflow" : "outflow"}" x="${x(i) - barWidth / 2}" y="${top}" width="${barWidth}" height="${Math.max(.8, Math.abs(y(value) - base))}" rx="${barWidth > 3 ? 1.5 : .5}"/>`;
    });
    if (z) {
      let path = ""; let open = false;
      series.forEach((row, i) => { if (finite(row.z_score)) { path += `${open ? "L" : "M"}${x(i).toFixed(2)},${zy(row.z_score).toFixed(2)} `; open = true; } else open = false; });
      svg += `<path class="z-path" d="${path}"/>`;
    }
    const labelCount = Math.min(series.length, compact ? 4 : 7);
    const labelled = new Set();
    for (let j = 0; j < labelCount; j++) {
      const i = labelCount === 1 ? 0 : Math.round((series.length - 1) * j / (labelCount - 1));
      if (!labelled.has(i)) svg += `<text class="x-label" x="${x(i)}" y="${height - 11}" text-anchor="middle">${esc(shortDate(series[i].date ?? series[i].trade_date))}</text>`;
      labelled.add(i);
    }
    svg += `<line class="hover-guide" x1="0" x2="0" y1="${margin.top}" y2="${height - margin.bottom}"/>`;
    series.forEach((row, i) => { svg += `<rect class="hit-zone" data-index="${i}" x="${margin.left + i * step}" y="${margin.top}" width="${step}" height="${plotHeight}"/>`; });
    svg += "</svg>";
    container.innerHTML = svg;
    const guide = container.querySelector(".hover-guide");
    container.querySelectorAll(".hit-zone").forEach((zone) => {
      const show = (event) => {
        const index = Number(zone.dataset.index);
        const row = series[index];
        guide.setAttribute("x1", x(index)); guide.setAttribute("x2", x(index)); guide.style.display = "block";
        const title = dateString(row.date ?? row.trade_date) || "日期未知";
        let content = `<div class="tooltip-date">${esc(title)}</div>`;
        if (shares) content += `<div class="tooltip-row"><span>基金总份额</span><strong>${number(row.total_share_yi)} 亿份</strong></div>`;
        else content += `<div class="tooltip-row"><span>估算净申赎</span><strong class="${colorClass(row.flow_yi)}">${number(row.flow_yi, 2, true)} 亿元</strong></div><div class="tooltip-row"><span>异常度 Z</span><strong>${number(row.z_score, 2, true)}</strong></div>`;
        if (finite(row.valid_count)) content += `<div class="tooltip-row"><span>有效 ETF</span><strong>${number(row.valid_count, 0)} / ${state.snapshot?.summary?.total_count ?? "—"}</strong></div>`;
        if (finite(row.delta_share_yi)) content += `<div class="tooltip-row"><span>份额变化</span><strong>${number(row.delta_share_yi, 2, true)} 亿份</strong></div>`;
        if (!shares && !finite(row.z_score)) content += `<div class="tooltip-small">Z 值未知：历史不足、数据缺失或零方差。</div>`;
        const tooltip = $("chartTooltip"); tooltip.innerHTML = content; tooltip.hidden = false;
        const tooltipRect = tooltip.getBoundingClientRect();
        const clientX = event.clientX ?? (container.getBoundingClientRect().left + x(index));
        const clientY = event.clientY ?? container.getBoundingClientRect().top;
        tooltip.style.left = `${Math.max(8, Math.min(window.innerWidth - tooltipRect.width - 10, clientX + 15))}px`;
        tooltip.style.top = `${Math.max(8, Math.min(window.innerHeight - tooltipRect.height - 10, clientY - tooltipRect.height - 12))}px`;
      };
      zone.addEventListener("pointermove", show);
      zone.addEventListener("pointerdown", show);
    });
    container.onpointerleave = () => { $("chartTooltip").hidden = true; if (guide) guide.style.display = "none"; };
  }

  function sparkline(series, field = "flow_yi", mode = "bars") {
    const values = series.map((row) => num(row[field]));
    const valid = values.filter((value) => value !== null);
    if (!valid.length) return `<svg viewBox="0 0 80 28" aria-label="暂无历史数据"><path d="M0 14h80" stroke="#e2e9d9" stroke-width="1" stroke-dasharray="2 3"/></svg>`;
    const width = 80; const height = 26;
    const max = Math.max(...valid.map(Math.abs), .01);
    const step = width / values.length;
    if (mode === "line") {
      let cumulative = 0;
      const accumulated = values.map((value) => value === null ? null : cumulative += value);
      const min = Math.min(...accumulated.filter((v) => v !== null), 0);
      const maxVal = Math.max(...accumulated.filter((v) => v !== null), 0);
      const y = (value) => 3 + (maxVal - value) / (maxVal - min || 1) * 20;
      let path = ""; let open = false;
      accumulated.forEach((value, i) => { if (value === null) { open = false; return; } path += `${open ? "L" : "M"}${((i + .5) * step).toFixed(1)},${y(value).toFixed(1)} `; open = true; });
      const color = cumulative >= 0 ? "#d09a80" : "#84aa8c";
      return `<svg viewBox="0 0 ${width} ${height}" aria-hidden="true"><path d="M0 24H80" stroke="#eef2e7" stroke-width=".8"/><path d="${path}" stroke="${color}" stroke-width="1.3" fill="none" stroke-linejoin="round"/></svg>`;
    }
    return `<svg viewBox="0 0 ${width} ${height}" aria-label="近 ${series.length} 个交易日资金变化"><path d="M0 13h80" stroke="#e9efdf" stroke-width=".8"/>${values.map((value, i) => value === null ? "" : `<rect x="${i * step + step * .15}" y="${value >= 0 ? 13 - Math.abs(value) / max * 11 : 13}" width="${Math.max(.8, step * .7)}" height="${Math.max(.7, Math.abs(value) / max * 11)}" rx=".5" fill="${value >= 0 ? "#d6a48a" : "#a5bea0"}"/>`).join("")}</svg>`;
  }

  function etfSeries(code) {
    const entries = state.snapshot?.etf_series ?? {};
    const rows = array(entries[code] ?? entries[code.split(".")[0]]);
    return state.selectedDate ? rows.slice(0, RadarTimeline.upperBound(rows, state.selectedDate)) : rows;
  }

  function renderTableFilters() {
    const select = $("indexFilter");
    const indexes = [...new Set(array(state.snapshot?.etfs).map((etf) => etf.index ?? etf.index_name).filter(Boolean))];
    select.innerHTML = '<option value="all">全部指数</option>' + indexes.map((index) => `<option value="${esc(index)}">${esc(index)}</option>`).join("");
    if (!indexes.includes(state.index)) state.index = "all";
    select.value = state.index;
  }

  function renderTable() {
    const allEtfs = array(state.snapshot?.etfs);
    let etfs = allEtfs.filter((etf) => (state.index === "all" || (etf.index ?? etf.index_name) === state.index) && (!state.search || `${codeOf(etf)} ${etf.name} ${etf.index ?? ""}`.toLowerCase().includes(state.search)));
    etfs.sort((a, b) => {
      const x = a[state.sort]; const y = b[state.sort];
      if (x === null || x === undefined) return y === null || y === undefined ? 0 : 1;
      if (y === null || y === undefined) return -1;
      return (finite(x) && finite(y) ? Number(x) - Number(y) : String(x).localeCompare(String(y), "zh-CN")) * state.sortDirection;
    });
    $("watchlistCount").textContent = `${allEtfs.length} 只`;
    if (!etfs.length) {
      $("etfTableBody").innerHTML = `<tr><td colspan="8"><div class="table-empty">${allEtfs.length ? "没有匹配的 ETF，试试其他代码、名称或指数。" : "尚未取得 ETF 份额数据，更新真实数据后查看。"}</div></td></tr>`;
    } else $("etfTableBody").innerHTML = etfs.map((etf) => {
      const code = codeOf(etf);
      const info = signalInfo(etf.status, etf);
      const quality = Array.isArray(etf.quality) ? etf.quality : etf.quality ? [etf.quality] : [];
      const indexName = etf.index ?? etf.index_name ?? "宽基 ETF";
      const indexNumber = String(indexName).match(/\d+/)?.[0] ?? (String(indexName).includes("50") ? "50" : "宽基");
      return `<tr tabindex="0" role="button" data-code="${esc(code)}" aria-label="查看${esc(etf.name ?? code)}详情"><td><div class="etf-name"><span class="etf-symbol">${esc(indexNumber)}</span><div><div class="etf-name-text">${esc(etf.name ?? code)}${quality.length ? `<span class="quality-dot" title="${esc(quality.join("；"))}"></span>` : ""}</div><div class="etf-subline"><span class="etf-code">${esc(code)}</span><span class="index-tag">${esc(indexName)}</span></div></div></div></td><td class="align-right"><div class="number">${number(etf.total_share_yi)}</div><div class="cell-sub">${shortDate(etf.date ?? state.snapshot.as_of)}</div></td><td class="align-right"><div class="number ${colorClass(etf.delta_share_yi)}">${number(etf.delta_share_yi, 2, true)}</div><div class="cell-sub">${percent(etf.share_change_pct)}</div></td><td class="align-right"><span class="number ${colorClass(etf.flow_yi)}">${number(etf.flow_yi, 2, true)}</span></td><td class="align-right"><span class="number ${finite(etf.z_score) && Math.abs(etf.z_score) >= state.params.z_threshold ? colorClass(etf.z_score) : ""}">${number(etf.z_score, 2, true)}</span></td><td><div class="table-spark">${sparkline(etfSeries(code).slice(-20))}</div></td><td><span class="status-badge ${info.kind}">${esc(info.short)}</span></td><td class="table-chevron"><span class="row-chevron">›</span></td></tr>`;
    }).join("");
    $("tableFooterText").textContent = `显示 ${etfs.length} / ${allEtfs.length} 只 ETF · 点击基金行查看历史份额与资金详情`;
    document.querySelectorAll("[data-sort]").forEach((button) => {
      const active = button.dataset.sort === state.sort;
      button.classList.toggle("active", active);
      button.querySelector("span").textContent = active ? (state.sortDirection < 0 ? "↓" : "↑") : "";
      button.closest("th").setAttribute("aria-sort", active ? (state.sortDirection < 0 ? "descending" : "ascending") : "none");
    });
  }

  function renderAlerts() {
    const confirmed = array(state.snapshot?.alerts);
    const observations = array(state.snapshot?.observation_events).map((entry) => ({ ...entry, kind: "synchrony_observation" }));
    const signature = `${confirmed.length}/${observations.length}/${state.alertsExpanded}`;
    if (renderAlerts.dataset === state.dataset && renderAlerts.signature === signature) return;
    renderAlerts.dataset = state.dataset; renderAlerts.signature = signature;
    const alerts = [...confirmed, ...observations].sort((a, b) => String(b.date).localeCompare(String(a.date)));
    $("alertCount").textContent = `${confirmed.length} 异动${observations.length ? ` · ${observations.length} 观察` : ""}`;
    $("showAllAlerts").hidden = alerts.length <= 4;
    $("showAllAlerts").innerHTML = state.alertsExpanded ? '收起记录 <span>↑</span>' : '查看全部 <span>↗</span>';
    if (!alerts.length) { $("alertsList").innerHTML = `<div class="small-empty">当前区间没有同时达到监测门槛的异动。<br>可调整日期或参数继续观察。</div>`; return; }
    $("alertsList").innerHTML = (state.alertsExpanded ? alerts : alerts.slice(0, 4)).map((alert) => {
      const observation = alert.kind === "synchrony_observation";
      const out = /out|净赎回|负|流出/.test(String(alert.direction)) || (!observation && Number(alert.flow_yi) < 0);
      return `<div class="alert-item ${observation ? "observation-event" : ""}" role="button" tabindex="0" data-alert-date="${esc(dateString(alert.date))}" title="查看此交易日的组合与 ETF 数据"><time class="alert-date">${esc(dateString(alert.date))}</time><span class="alert-marker ${observation ? "observation" : out ? "outflow" : ""}">${observation ? "◌" : out ? "↘" : "↗"}</span><div class="alert-copy"><div class="alert-title">${esc(alert.title ?? (out ? "大额净赎回异动" : "大额净申购异动"))}${observation ? '<span class="observation-label">观察</span>' : ""}</div><div class="alert-detail">${observation ? `${number(alert.sync_count, 0)} 只同向达标 · 组合条件未齐` : `Z ${number(alert.z_score, 2, true)} · ${number(alert.sync_count, 0)} 只 ETF 同步${out ? "净赎回" : "净申购"}`}</div></div><div><div class="alert-flow ${colorClass(alert.flow_yi)}">${number(alert.flow_yi, 2, true)}</div><div class="alert-flow-unit">${observation ? "组合净额 · " : ""}亿元</div></div></div>`;
    }).join("");
  }

  function renderEvidence(selectionOnly = false) {
    const data = state.snapshot ?? {};
    const evidence = array(data.evidence);
    const monitoring = data.monitoring ?? {};
    const sources = Object.values(monitoring.official_sources ?? {});
    const healthy = sources.filter((s) => s.state === "ok").length;
    $("evidenceStatus").querySelector("p").textContent = sources.length ? `官方公告源 ${healthy} / ${sources.length} 本次可访问 · ${evidence.length} 条截止日前披露。公告不分配当日 ETF 交易额。` : "公告采集尚未运行；未抓取不等于没有增持。";
    const evidenceHtml = evidence.slice(0, 5).map((entry) => {
      const url = safeUrl(entry.url);
      const title = esc(entry.title ?? "公开披露");
      const official = entry.verification === "official_source";
      const details = [entry.excerpt, entry.first_seen_at ? `首次采集：${entry.first_seen_at}；历史回看仅按公告日过滤` : entry.verification].filter(Boolean).join("\n");
      return `<div class="evidence-item" title="${esc(details)}"><span class="evidence-type">${esc(official ? entry.source_name ?? "官方" : "人工")}</span>${url ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${title} ↗</a>` : `<span>${title}</span>`}<time title="公告发布日期">${esc(dateString(entry.date))}</time></div>`;
    }).join("");
    if ($("evidenceList").innerHTML !== evidenceHtml) $("evidenceList").innerHTML = evidenceHtml;
    const coverageNode = $("monitoringCoverage");
    const labels = { ok: "已接入", connected: "自动采集", implemented: "已实现", partial: "部分可用", unavailable: "暂不可用", not_fetched: "尚未获取", stale_cache: "旧缓存", not_implemented: "未实现" };
    if (coverageNode && !selectionOnly) coverageNode.innerHTML = `<h3>监测手段覆盖</h3><p>状态按真实采集结果更新；辅助线索不自动加分。</p>${array(monitoring.channels).map((c) => `<div class="coverage-method"><div><strong>${esc(c.name)}</strong><span class="coverage-state ${["ok", "connected", "implemented"].includes(c.state) ? "ok" : "warn"}">${esc(labels[c.state] ?? c.state)}</span></div><p>${esc(c.scope ?? "")}</p></div>`).join("")}<details class="source-status"><summary>官方源状态与采集时间</summary>${sources.map((s) => `<p><strong>${esc(s.name)}</strong> · ${esc(labels[s.state] ?? s.state)}<br>${esc(s.checked_at ?? "尚未采集")}${s.error ? ` · ${esc(s.error)}` : ""}</p>`).join("")}</details>`;
    const crossNode = $("crosscheckObservations");
    if (crossNode) {
    const opened = new Set([...crossNode.querySelectorAll("details[open]")].map((node) => node.dataset.channel));
    const html = Object.entries(data.crosschecks?.channels ?? {}).map(([key, channel]) => {
      const detail = (row) => key === "holders" ? `${row.holder_name} · 报告期 ${row.report_period} · ${number(finite(row.hold_amount_shares) ? row.hold_amount_shares / 1e8 : null)} 亿股` : key === "dragon_tiger" ? `${row.seat} · 席位净额 ${number(finite(row.net_buy_yuan) ? row.net_buy_yuan / 1e8 : null, 2, true)} 亿元` : key === "block_trades" ? `成交 ${number(row.amount_yi)} 亿元 · 每笔含买卖双方` : `超大单主动买卖差额 ${number(row.elg_active_difference_yi, 2, true)} 亿元`;
      return `<details class="crosscheck-channel" data-channel="${esc(key)}"${opened.has(key) ? " open" : ""}><summary>${esc(channel.name)} <span>${esc(labels[channel.state] ?? channel.state)} · ${number(channel.total_records ?? 0, 0)} 条</span></summary><p>${esc(channel.limit)}${channel.review_cutoff ? " · 已采集记录按所选交易日截止，不代表更早历史已完整采集。" : ""}</p>${array(channel.rows).slice(0, 5).map((row) => `<div class="crosscheck-row"><time>${esc(row.date)}</time><strong>${esc(row.ts_code)}</strong><span>${esc(detail(row))}</span></div>`).join("") || '<p>当前没有可展示记录。来源不可用时不按0处理。</p>'}${array(channel.errors).length ? `<p class="crosscheck-error">${channel.errors.map(esc).join("；")}</p>` : ""}</details>`;
    }).join("");
    if (crossNode.innerHTML !== html) crossNode.innerHTML = html;
    }
    const quality = data.quality ?? {};
    const meta = data.metadata ?? {};
    let warnings = [...array(quality.warnings), ...array(meta.warnings), ...array(meta.errors)].map((value) => typeof value === "object" ? (value.message ?? JSON.stringify(value)) : String(value)).filter(Boolean);
    warnings = [...new Set(warnings)];
    const valid = data.summary?.valid_count ?? 0;
    const total = data.summary?.total_count ?? array(data.etfs).length;
    $("qualitySummary").textContent = meta.is_stale ? "缓存数据 · 待更新" : `${valid} / ${total} 有效${warnings.length ? ` · ${warnings.length} 项提醒` : ""}`;
    $("qualityIndicator").className = `quality-indicator ${warnings.length || meta.is_stale || valid < total ? "warn" : ""}`;
    const fields = [
      ["最新已披露份额", quality.latest_share_date ?? meta.data_end ?? data.as_of],
      ["最新行情日期", quality.latest_quote_date],
      ["最近应有交易日", meta.expected_latest_trade_date],
    ].filter(([, value]) => value);
    let html = `<div class="quality-row"><span>当前组合有效覆盖</span><span>${valid} / ${total} 只 ETF</span></div>`;
    html += fields.map(([label, value]) => `<div class="quality-row"><span>${esc(label)}</span><span class="numeric">${esc(dateString(value) || value)}</span></div>`).join("");
    if (array(quality.pending_dates).length) html += `<div class="quality-row"><span>待披露日期</span><span>${quality.pending_dates.map((date) => esc(dateString(date) || date)).join("、")}</span></div>`;
    if (warnings.length) html += `<ul class="quality-warnings">${warnings.map((warning) => `<li>${esc(warning)}</li>`).join("")}</ul>`;
    else html += '<p style="margin-top:7px">当前未发现额外的数据质量提醒。缺失份额或净值不会按 0 计算。</p>';
    $("qualityContent").innerHTML = html;
    if (meta.is_stale) $("qualityDetails").open = true;
  }

  function renderMethod() {
    const data = state.snapshot ?? {};
    const sourceList = array(data.sources).length ? data.sources : array(state.config.sources);
    const schedule = data.schedule ?? state.config.schedule ?? {};
    const sources = sourceList.map((source) => {
      const url = safeUrl(source.url); return url ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(source.title ?? "官方文档")} ↗</a>` : esc(source.title ?? "");
    }).filter(Boolean).join('<span class="source-divider">·</span>');
    $("methodSource").innerHTML = `<div>数据来源：本地 Tushare 认证 · 交易日披露的基金份额、单位净值与行情</div>${sources ? `<div class="method-links">${sources}</div>` : ""}<div class="schedule-note"><span class="schedule-dot ${schedule.enabled ? "enabled" : ""}"></span>${schedule.enabled ? `自动更新：工作日北京时间 ${array(schedule.times).map(esc).join("、") || "按本地配置"}${schedule.requires_running ? " · 服务运行期间生效" : ""}` : "自动更新未启用 · 可通过顶部按钮手动更新真实数据"}</div>`;
    $("methodThresholds").textContent = `以前 ${state.params.window} 个交易日（须完整）为基准计算 Z-Score，缺失时不向更早日期借样本。组合资金绝对值 ≥ ${number(state.params.min_flow_yi, 1)} 亿元、|Z| ≥ ${number(state.params.z_threshold, 1)} 且资金与 Z 方向一致，同时至少 ${state.params.sync_min} 只 ETF 各自达到同方向的金额与 Z 门槛时提示异动。仅同步达标时列为观察，金额始终显示组合实际净额。历史不足、缺失或零方差时 Z 显示为「—」。`;
    const footerStatus = document.querySelector(".page-footer > span");
    if (footerStatus) {
      footerStatus.textContent = schedule.enabled ? `工作日 ${array(schedule.times).join(" / ")} 自动更新 · 需服务运行` : "本地运行 · 手动更新";
      footerStatus.title = schedule.requires_running ? "保持本地服务运行，自动更新才会执行。" : "本地研究观察工具";
    }
  }

  function openDrawer(code) {
    state.selectedEtf = code; state.drawerMode = "flow"; state.previousFocus = document.activeElement;
    $("drawerBackdrop").hidden = false; $("detailDrawer").hidden = false; document.body.classList.add("modal-open");
    renderDrawer(); $("drawerClose").focus();
  }

  function closeDrawer() {
    $("detailDrawer").hidden = true; $("drawerBackdrop").hidden = true; $("chartTooltip").hidden = true;
    state.selectedEtf = null;
    if (!$("settingsDialog").open) document.body.classList.remove("modal-open");
    state.previousFocus?.focus?.();
  }

  function renderDrawer() {
    const etf = array(state.snapshot?.etfs).find((entry) => codeOf(entry) === state.selectedEtf);
    if (!etf) { closeDrawer(); return; }
    const info = signalInfo(etf.status, etf);
    const series = etfSeries(state.selectedEtf).slice(-252);
    const recent = series.slice(-10).reverse();
    const metadata = state.snapshot.metadata?.symbols?.[state.selectedEtf] ?? {};
    const warnings = [...new Set([...(Array.isArray(etf.quality) ? etf.quality : etf.quality ? [etf.quality] : []), ...array(metadata.warnings), ...array(metadata.errors)])];
    $("drawerContent").innerHTML = `<h2 id="drawerTitle">${esc(etf.name ?? state.selectedEtf)}</h2><div class="drawer-subtitle"><span class="etf-code">${esc(state.selectedEtf)}</span><span>·</span><span>${esc(etf.index ?? etf.index_name ?? "宽基 ETF")}</span><span>·</span><span>${esc(dateString(etf.date ?? state.snapshot.as_of))}</span></div><div class="drawer-value"><div class="drawer-value-label">当日估算净申赎<span class="status-badge ${info.kind}">${esc(info.short)}</span></div><div class="drawer-big-number ${colorClass(etf.flow_yi)}">${number(etf.flow_yi, 2, true)}<small>亿元</small></div><div class="drawer-value-foot"><span>份额变化 × 前日净值</span><span>Z ${number(etf.z_score, 2, true)}</span></div></div><div class="drawer-metrics"><div class="drawer-metric"><label>基金总份额</label><strong>${number(etf.total_share_yi)}</strong><small>亿份</small></div><div class="drawer-metric"><label>净增份额</label><strong class="${colorClass(etf.delta_share_yi)}">${number(etf.delta_share_yi, 2, true)}</strong><small>亿份</small></div><div class="drawer-metric"><label>份额日变化</label><strong class="${colorClass(etf.share_change_pct)}">${number(etf.share_change_pct, 2, true)}</strong><small>%</small></div></div><div class="drawer-section-label"><h3>历史资金与份额</h3><span>${series.length} 个交易日</span></div><div class="drawer-tabbar"><button data-drawer-tab="flow" class="${state.drawerMode === "flow" ? "active" : ""}">估算净申赎</button><button data-drawer-tab="shares" class="${state.drawerMode === "shares" ? "active" : ""}">基金总份额</button></div><div class="chart-container drawer-chart" id="drawerChart"></div><div class="drawer-section-label"><h3>最近交易日</h3><span>净申赎 / 亿元 · 份额 / 亿份</span></div><table class="drawer-history"><thead><tr><th>日期</th><th>总份额</th><th>份额变化</th><th>估算净申赎</th><th>Z 值</th></tr></thead><tbody>${recent.map((row) => `<tr><td>${esc(shortDate(row.date))}</td><td>${number(row.total_share_yi)}</td><td class="${colorClass(row.delta_share_yi)}">${number(row.delta_share_yi, 2, true)}</td><td class="${colorClass(row.flow_yi)}">${number(row.flow_yi, 2, true)}</td><td>${number(row.z_score, 2, true)}</td></tr>`).join("") || '<tr><td colspan="5">暂无历史数据</td></tr>'}</tbody></table><div class="drawer-quality"><strong>数据质量与来源</strong><div>${metadata.mode === "stale_cache" ? "使用历史缓存，数据更新尚未完成" : metadata.mode === "live" ? "本次更新已从 Tushare 获取数据" : "本地缓存 · Tushare 历史披露"}</div>${metadata.share_end ? `<div>份额截至 ${esc(dateString(metadata.share_end))}</div>` : ""}${warnings.length ? `<ul>${warnings.map((warning) => `<li>${esc(typeof warning === "object" ? warning.message ?? JSON.stringify(warning) : warning)}</li>`).join("")}</ul>` : '<div>当前未发现额外的数据质量提醒。</div>'}</div><p class="drawer-note">净申购反映新增份额需求，无法据此识别交易主体。盘中 ETF 价格和成交额与清算后基金总份额属于不同口径。</p>`;
    drawChart($("drawerChart"), series, { field: state.drawerMode === "shares" ? "total_share_yi" : "flow_yi", shares: state.drawerMode === "shares", z: state.drawerMode === "flow" });
    $("drawerContent").querySelectorAll("[data-drawer-tab]").forEach((button) => button.addEventListener("click", () => { state.drawerMode = button.dataset.drawerTab; renderDrawer(); }));
  }

  function viewDate(value) {
    const date = dateString(value);
    if (!date) return;
    if (state.timeline?.index.has(date)) {
      const visible = state.chartController?.dateIndex.has(date);
      if (!visible) {
        state.range = "all"; syncRangeControls(); renderMainChart();
      }
      selectTradingDate(date, { persist: true });
      $("overview").scrollIntoView({ behavior: "smooth" });
      return;
    }
    if ($("startDate").value && $("startDate").value > date) $("startDate").value = date;
    $("endDate").value = date;
    const params = readDateDraft();
    if (params) { loadSnapshot({ mode: "custom", params }); $("overview").scrollIntoView({ behavior: "smooth" }); }
  }

  function openSettings() {
    if (![...$("settingWindow").options].some((option) => Number(option.value) === state.params.window)) {
      const option = document.createElement("option"); option.value = state.params.window; option.textContent = `${state.params.window} 日`; $("settingWindow").appendChild(option);
    }
    $("settingWindow").value = String(state.params.window);
    $("settingZ").value = state.params.z_threshold;
    $("settingFlow").value = state.params.min_flow_yi;
    $("settingSync").value = state.params.sync_min;
    $("settingsDialog").showModal(); document.body.classList.add("modal-open");
  }

  function closeSettings() { $("settingsDialog").close(); if (!state.selectedEtf) document.body.classList.remove("modal-open"); }

  function syncRangeControls() {
    $("rangeControls").querySelectorAll("button").forEach((button) => { const active = button.dataset.range === state.range; button.classList.toggle("active", active); button.setAttribute("aria-pressed", active ? "true" : "false"); });
  }

  function bindEvents() {
    const submitDates = () => { const params = readDateDraft(); if (params) loadSnapshot({ mode: "custom", params }); };
    $("dateApply").addEventListener("click", submitDates);
    ["startDate", "endDate"].forEach((id) => {
      $(id).addEventListener("keydown", (event) => { if (event.key === "Enter") submitDates(); });
      $(id).addEventListener("input", () => { state.dateDraftDirty = true; $("dateDraftNote").textContent = "日期尚未应用；当前图表和导出保持原区间。"; });
    });
    $("viewModeControls").addEventListener("click", (event) => {
      const button = event.target.closest("[data-view]"); if (!button) return;
      if (button.dataset.view === "custom") { state.dateEditorOpen = !state.dateEditorOpen; renderViewChrome(); if (state.dateEditorOpen) $("startDate").focus(); }
      else loadSnapshot({ mode: button.dataset.view });
    });
    $("exportButton").addEventListener("click", (event) => { if (!state.snapshot) { event.preventDefault(); notify("真实数据载入后可导出当前视图。"); } });
    $("refreshButton").addEventListener("click", refreshData);
    $("noticeClose").addEventListener("click", () => $("notice").hidden = true);
    ["railSettings", "topSettings", "chartSettings"].forEach((id) => $(id).addEventListener("click", openSettings));
    $("settingsClose").addEventListener("click", closeSettings);
    $("settingsDialog").addEventListener("close", () => { if (!state.selectedEtf) document.body.classList.remove("modal-open"); });
    $("settingsDialog").addEventListener("click", (event) => { const rect = $("settingsDialog").getBoundingClientRect(); if (event.target === $("settingsDialog") && (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom)) closeSettings(); });
    $("settingsReset").addEventListener("click", () => { $("settingWindow").value = DEFAULTS.window; $("settingZ").value = DEFAULTS.z_threshold; $("settingFlow").value = DEFAULTS.min_flow_yi; $("settingSync").value = DEFAULTS.sync_min; });
    $("settingsForm").addEventListener("submit", (event) => {
      event.preventDefault();
      if (!$("settingsForm").reportValidity()) return;
      const params = { ...state.params, window: Number($("settingWindow").value), z_threshold: Number($("settingZ").value), min_flow_yi: Number($("settingFlow").value), sync_min: Number($("settingSync").value) };
      closeSettings(); loadSnapshot({ params });
    });
    $("rangeControls").addEventListener("click", (event) => {
      const button = event.target.closest("[data-range]"); if (!button) return;
      state.range = button.dataset.range;
      syncRangeControls();
      renderMainChart(); if (state.snapshot) persistView();
    });
    for (const [id, delta] of [["previousTradeDate", -1], ["nextTradeDate", 1]]) $(id).addEventListener("click", () => {
      const controller = state.chartController;
      const i = controller?.dateIndex.get(state.selectedDate);
      const row = controller?.series[(i ?? -1) + delta];
      if (row) selectTradingDate(row.date, { persist: true });
    });
    $("returnLatestDate").addEventListener("click", () => selectTradingDate(state.dataset?.as_of, { persist: true }));
    $("historyRetry").addEventListener("click", () => loadHistory());
    $("indexToggle").addEventListener("click", () => {
      if ($("indexToggle").disabled) return;
      state.indexVisible = !state.indexVisible;
      renderMainChart();
      if (state.snapshot) persistView();
    });
    $("etfSearch").addEventListener("input", (event) => { state.search = event.target.value.trim().toLowerCase(); renderTable(); });
    $("indexFilter").addEventListener("change", (event) => { state.index = event.target.value; renderTable(); });
    document.querySelectorAll("[data-sort]").forEach((button) => button.addEventListener("click", () => { const sort = button.dataset.sort; state.sortDirection = state.sort === sort ? -state.sortDirection : sort === "name" || sort === "status" ? 1 : -1; state.sort = sort; renderTable(); }));
    $("etfTableBody").addEventListener("click", (event) => { const row = event.target.closest("[data-code]"); if (row) openDrawer(row.dataset.code); });
    $("etfTableBody").addEventListener("keydown", (event) => { const row = event.target.closest("[data-code]"); if (row && ["Enter", " "].includes(event.key)) { event.preventDefault(); openDrawer(row.dataset.code); } });
    $("drawerClose").addEventListener("click", closeDrawer); $("drawerBackdrop").addEventListener("click", closeDrawer);
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && state.selectedEtf) { event.preventDefault(); closeDrawer(); }
      if (event.key === "Tab" && state.selectedEtf && !$("settingsDialog").open) {
        const focusable = [...$("detailDrawer").querySelectorAll('button, a[href], input, select, [tabindex="0"]')].filter((el) => !el.disabled && el.offsetParent !== null);
        const first = focusable[0]; const last = focusable.at(-1);
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
      }
    });
    $("showAllAlerts").addEventListener("click", () => { state.alertsExpanded = !state.alertsExpanded; renderAlerts(); });
    $("alertsList").addEventListener("click", (event) => { const row = event.target.closest("[data-alert-date]"); if (row) viewDate(row.dataset.alertDate); });
    $("alertsList").addEventListener("keydown", (event) => { const row = event.target.closest("[data-alert-date]"); if (row && ["Enter", " "].includes(event.key)) { event.preventDefault(); viewDate(row.dataset.alertDate); } });
    document.querySelectorAll(".rail nav a").forEach((link) => link.addEventListener("click", () => { document.querySelectorAll(".rail nav a").forEach((item) => item.classList.toggle("active", item === link)); }));
    let resizeTimer;
    window.addEventListener("resize", () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(() => { if (state.snapshot) { renderMainChart(); if (state.selectedEtf) renderDrawer(); } }, 120); });
  }

  async function init() {
    state.progressive = true;
    bindEvents();
    // Unknown values remain unknown: the UI never manufactures market observations.
    try {
      state.config = await api("/api/config");
      const defaults = state.config.defaults ?? state.config.config ?? state.config;
      for (const key of Object.keys(DEFAULTS)) if (finite(defaults[key])) state.params[key] = Number(defaults[key]);
      renderMethod();
    } catch { /* The snapshot can still be read if configuration metadata is unavailable. */ }
    const saved = readUrlState();
    state.params = { ...state.params, ...saved.params };
    state.mode = saved.mode; state.range = saved.range; state.indexVisible = saved.indexVisible;
    state.restoreDate = saved.date || null;
    state.baseStart = saved.params.start_date ?? state.config.start_date;
    syncDateInputs();
    if (state.config.today) $("endDate").max = state.config.today;
    syncRangeControls();
    await loadSnapshot();
    await pollStatus();
  }

  init();
})();
