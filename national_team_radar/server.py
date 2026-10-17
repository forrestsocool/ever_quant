"""Local dashboard and background refresh coordinator.

No credentials are sent to the browser. The provider owns authentication and
raw-data caches; this module only persists market data and analytical results.
"""
from __future__ import annotations

import argparse
from collections import OrderedDict
from copy import deepcopy
import csv
import io
import json
import math
import os
import threading
import tempfile
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware
from pydantic import BaseModel

from .engine import analyze
from .provider import ETF_UNIVERSE, COMMON_LIST_DATE, EARLIEST_DATA_DATE, fetch_market_data

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
CN = timezone(timedelta(hours=8))
DEFAULTS = {"window": 20, "z_threshold": 2.0, "min_flow_yi": 10.0, "sync_min": 3}
SOURCE_LINKS = [
    {"title": "上交所：ETF 的一级与二级市场机制", "url": "https://etf.sse.com.cn/fund/learning/knowledge/c/5704300.shtml"},
    {"title": "上交所：ETF 申购、赎回与份额创造", "url": "https://www.sse.com.cn/assortment/fund/etf/question/"},
    {"title": "Tushare：基金份额（万份）", "url": "https://tushare.pro/document/2?doc_id=207"},
    {"title": "Tushare：ETF 份额规模与披露时点", "url": "https://tushare.pro/document/2?doc_id=408"},
]


def cn_now() -> datetime:
    return datetime.now(CN)


def clean_json(value: Any) -> Any:
    if value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, dict):
        return {str(k): clean_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean_json(v) for v in value]
    if isinstance(value, (datetime, date, pd.Timestamp)):
        return value.isoformat()
    if hasattr(value, "item"):
        return clean_json(value.item())
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def atomic_json(path: Path, content: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(clean_json(content), ensure_ascii=False, allow_nan=False)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(payload)
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def data_updated_at(metadata: dict) -> str | None:
    """Latest disclosures use the oldest live-source clock; history has its own clock."""
    timestamps = []
    for group in (metadata.get("symbols", {}), metadata.get("auxiliary", {})):
        for detail in group.values():
            if detail.get("fetched_at") and detail.get("mode") != "unavailable":
                try:
                    timestamps.append(datetime.fromisoformat(detail["fetched_at"]))
                except (ValueError, TypeError):
                    pass
    return min(timestamps).isoformat(timespec="seconds") if timestamps else metadata.get("fetched_at")


def parse_dates(start: str | None, end: str | None) -> tuple[str, str]:
    try:
        finish = date.fromisoformat(end) if end else cn_now().date()
        begin = date.fromisoformat(start) if start else date.fromisoformat(COMMON_LIST_DATE)
    except ValueError as exc:
        raise ValueError("日期应为 YYYY-MM-DD") from exc
    if begin > finish:
        raise ValueError("开始日期不能晚于结束日期")
    if finish > cn_now().date():
        raise ValueError("结束日期不能晚于北京时间今天")
    if begin.isoformat() < EARLIEST_DATA_DATE:
        raise ValueError(f"历史查询起点不能早于现有基金的成立起点 {EARLIEST_DATA_DATE}")
    return begin.isoformat(), finish.isoformat()


def load_evidence() -> list[dict]:
    """Merge provenance-labelled official collection with optional manual notes."""
    from .disclosures import load_disclosures
    result = list(load_disclosures().get("items", []))
    path = DATA / "evidence.csv" if (DATA / "evidence.csv").exists() else ROOT / "evidence.csv"
    if not path.exists():
        return result
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    for row in rows:
        if row.get("date") and row.get("title"):
            try:
                date.fromisoformat(row["date"])
            except ValueError:
                continue
            if not row.get("url", "").startswith(("https://", "http://")):
                row["url"] = ""
            result.append({k: row.get(k, "") for k in ("date", "title", "url", "kind")})
    return result


def refresh_cross_validation(calendar: list[str], finish: str) -> None:
    from .disclosures import refresh_disclosures
    from .crosschecks import refresh_crosschecks
    # Each feed has its own TTL and explicit per-source failures.
    # An auxiliary endpoint cannot invalidate a usable ETF market snapshot.
    refresh_disclosures()
    refresh_crosschecks(calendar, finish)


def monitoring_coverage() -> dict:
    from .disclosures import load_disclosures
    from .crosschecks import load_crosschecks
    disclosures = load_disclosures()
    channels = load_crosschecks().get("channels", {})
    return {"official_sources": disclosures.get("sources", {}), "announcement_scope": disclosures.get("scope"),
            "channels": [
                {"id": "etf_flow", "name": "宽基ETF份额与净申赎", "state": "implemented", "scope": "七只固定宽基；是否可用以同日覆盖率为准"},
                {"id": "announcements", "name": "官方增减持公告", "state": "partial" if any(s.get("state") != "ok" for s in disclosures.get("sources", {}).values()) else "connected", "scope": "汇金、证金、社保官方栏目；近页扫描，分源报告失败"},
                *[{"id": key, "name": value.get("name"), "state": value.get("state", "not_fetched"), "scope": value.get("limit"), "checked_at": value.get("checked_at"), "records": value.get("total_records", 0)} for key, value in channels.items()],
                {"id": "etf_holders", "name": "ETF定期报告持有人归属", "state": "not_implemented", "scope": "尚未自动解析基金年报/半年报前十大持有人；不能把基金投资组合当作ETF持有人"},
                {"id": "intraday_iopv", "name": "盘中折溢价与IOPV", "state": "not_implemented", "scope": "当前仅有日频收盘价/净值；无盘中IOPV和实时主动买单数据"}],
            "identity_policy": "市场行为辅助项不自动提高归属置信度；公告仅佐证其披露的机构行为，不能分配当日ETF交易额"}


class RadarStore:
    def __init__(self, start: str | None = None, end: str | None = None):
        self.lock = threading.RLock()
        self.data: dict | None = None
        self._explicit_range = start is not None or end is not None
        self.defaults = parse_dates(start, end)
        self.state = {"running": False, "stage": "尚未更新", "error": None, "updated_at": None, "warning": None, "degraded": False}
        self.auto_update = False
        self._stop = threading.Event()
        self._scheduler_thread: threading.Thread | None = None
        self._scheduled_slot: str | None = None
        self.load()

    def load(self) -> None:
        path = DATA / "market_data.json"
        if not path.exists():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            raw["observations"] = pd.DataFrame(raw["observations"])
            for code, detail in (raw.get("metadata", {}).get("symbols", {})).items():
                warnings_text = " ".join(detail.get("warnings", []))
                mask = raw["observations"].get("ts_code", pd.Series(dtype=str)) == code
                if "日线行情使用互联网" in warnings_text:
                    raw["observations"].loc[mask, "amount"] = None
                if "基金净值使用互联网" in warnings_text:
                    # Legacy cache has no authentic NAV publication timestamp.
                    raw["observations"].loc[mask, "nav_ann_date"] = str(detail.get("fetched_at") or cn_now().date())[:10]
            if raw["observations"].empty or "trade_date" not in raw["observations"] or not raw.get("calendar"):
                raise ValueError("快照缺少必要的市场数据")
            if not isinstance(raw.get("metadata"), dict):
                raise ValueError("快照缺少数据来源信息")
            defaults = self.defaults
            if not self._explicit_range:
                defaults = parse_dates(None, None)
            updated_at = data_updated_at(raw["metadata"])
            self.data = raw
            self.defaults = defaults
            self.state.update(stage="已读取本地缓存", updated_at=updated_at,
                              degraded=raw["metadata"].get("refresh_health", "ok") != "ok")
        except (OSError, ValueError, KeyError, TypeError):
            self.data = None
            self.state.update(error="本地快照损坏，请重新更新", stage="等待更新")

    def refresh(self, start: str | None = None, end: str | None = None, force: bool = True) -> bool:
        with self.lock:
            if self.state["running"]:
                return False
            begin, finish = parse_dates(start if start is not None else self.defaults[0], end if end is not None else self.defaults[1])
            if not self._explicit_range:
                # A historical view must not shrink the persistent global pool.
                begin = min(begin, COMMON_LIST_DATE)
                finish = max(finish, cn_now().date().isoformat())
            self.state.update(running=True, stage="正在获取交易日历、ETF 份额、净值和行情…", error=None,
                              warning=None, last_attempt_at=cn_now().isoformat(timespec="seconds"))
        threading.Thread(target=self._fetch, args=(begin, finish, force), daemon=True, name="radar-refresh").start()
        return True

    def _fetch(self, begin: str, finish: str, force: bool) -> None:
        try:
            # Fetch enough prior sessions for any allowed 120-session window,
            # including Chinese exchange holidays. The user's display range is
            # independent of the underlying calculation warmup.
            warmup_start = max(EARLIEST_DATA_DATE, (date.fromisoformat(begin) - timedelta(days=270)).isoformat())
            fetched = fetch_market_data(warmup_start, finish, force=force)
            result = {**fetched, "observations": fetched["observations"].copy(),
                      "calendar": list(fetched["calendar"]), "metadata": deepcopy(fetched["metadata"])}
            frame = result["observations"]
            if frame.empty:
                raise ValueError("数据源没有返回可用记录")
            result["metadata"].update(requested_start_date=begin, requested_end_date=finish, fetched_start_date=warmup_start, fetched_end_date=finish)
            health = result["metadata"].get("refresh_health")
            if health is None:
                health = "stale" if result["metadata"].get("is_stale") else ("partial" if result["metadata"].get("errors") else "ok")
            result["metadata"]["refresh_health"] = health
            degraded = health != "ok"
            warning = "部分来源更新失败，保留可用数据；请查看数据源状态和各来源时间。" if degraded else None
            updated_at = data_updated_at(result["metadata"])
            completed_status = {**self.state, "running": False, "error": None, "degraded": degraded,
                                "warning": warning, "updated_at": updated_at,
                                "completed_at": cn_now().isoformat(timespec="microseconds"),
                                "stage": "更新完成（含旧缓存或缺失来源）" if degraded else "更新完成"}
            # Validate the candidate before publishing either in memory or on
            # disk. A missing calendar or analysis failure must preserve the
            # previous usable market snapshot.
            snapshot = self._snapshot_data(result, completed_status)
            try:
                with self.lock:
                    self.state["stage"] = "正在更新官方公告与交易辅助线索…"
                refresh_cross_validation(result["calendar"], finish)
            except Exception:
                # Never discard good ETF data for a failing corroboration feed.
                pass
            snapshot = self._snapshot_data(result, completed_status)
            raw = {**result, "observations": frame.to_dict(orient="records")}
            atomic_json(DATA / "snapshot.json", snapshot)
            self.write_alerts(snapshot)
            atomic_json(DATA / "market_data.json", raw)
            with self.lock:
                self.data = result
                self.defaults = (begin, finish)
                self.state.update({key: value for key, value in completed_status.items() if key != "running"})
        except Exception as exc:
            # Provider exceptions are sanitized; never expose unexpected transport
            # details which could contain an authenticated request payload.
            from .provider import __name__ as _provider_name
            known = type(exc).__module__ == _provider_name or isinstance(exc, ValueError)
            message = str(exc) if known else f"更新未完成（{type(exc).__name__}）；请检查本地认证、网络与数据接口。"
            with self.lock:
                self.state.update(error=message, stage="更新失败，保留上次数据")
        finally:
            with self.lock:
                self.state["running"] = False

    @staticmethod
    def write_alerts(snapshot: dict) -> None:
        path = DATA / "alerts.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8-sig", newline="", dir=path.parent, prefix=".alerts.", delete=False) as handle:
            temporary = Path(handle.name)
            fields = ["date", "direction", "flow_yi", "z_score", "sync_count", "title"]
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(snapshot.get("alerts", []))
        try:
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def snapshot(self, config: dict | None = None, start: str | None = None, end: str | None = None,
                 *, crosscheck_limit: int | None = 20) -> dict:
        with self.lock:
            if self.data is None:
                raise ValueError("还没有本地数据。点击“更新数据”，读取本地 Tushare 认证后获取真实数据。")
            data = self.data
            status = dict(self.state)
        return self._snapshot_data(data, status, config, start, end, crosscheck_limit=crosscheck_limit)

    def _snapshot_data(self, data: dict, status: dict, config: dict | None = None,
                       start: str | None = None, end: str | None = None,
                       *, crosscheck_limit: int | None = 20) -> dict:
        frame = data["observations"]
        calendar = data["calendar"][:]
        metadata = deepcopy(data["metadata"])
        if not calendar:
            raise ValueError("交易日历不可用，不能确认相邻交易日；请重新更新。")
        begin, finish = parse_dates(start or (metadata.get("requested_start_date") if self._explicit_range else self.defaults[0]), end or (metadata.get("requested_end_date") if self._explicit_range else self.defaults[1]))
        # Retain pre-range data as warmup, then trim output. Indicators never use
        # observations after the selected end date, avoiding historical lookahead.
        analysis_begin = max(EARLIEST_DATA_DATE, (date.fromisoformat(begin) - timedelta(days=270)).isoformat())
        dates = frame["trade_date"].astype(str)
        frame = frame.loc[(dates >= analysis_begin) & (dates <= finish)].copy()
        # NAV dates identify the valuation day, not the disclosure day. A
        # historical cutoff must not borrow a NAV announced after that cutoff.
        masked_navs = 0
        if "nav_ann_date" in frame.columns:
            ann_dates = pd.to_datetime(frame["nav_ann_date"].astype(str), errors="coerce").dt.strftime("%Y-%m-%d")
            not_yet_announced = ann_dates.notna() & (ann_dates > finish)
            masked_navs = int((not_yet_announced & frame["nav"].notna()).sum())
            frame.loc[not_yet_announced, "nav"] = None
        calendar = [day for day in calendar if analysis_begin <= day <= finish]
        result = analyze(frame, calendar, ETF_UNIVERSE, config={**DEFAULTS, **(config or {})}, evidence=load_evidence())
        if not result.get("as_of") or result["as_of"] < begin:
            raise ValueError("所选区间没有已披露的份额数据，请调整日期或点击更新获取该区间。")
        result["series"] = [x for x in result.get("series", []) if x["date"] >= begin]
        result["alerts"] = [x for x in result.get("alerts", []) if begin <= x["date"] <= finish]
        result["observation_events"] = [x for x in result.get("observation_events", []) if begin <= x["date"] <= finish]
        for code, rows in result.get("etf_series", {}).items():
            result["etf_series"][code] = [x for x in rows if x["date"] >= begin]
        metadata.update(view_start_date=begin, view_end_date=finish)
        metadata["navs_excluded_after_cutoff"] = masked_navs
        metadata["historical_data_policy"] = "按净值公告日期限制截止日可见性；来源历史修订及份额披露时刻未重建，不作严格历史时点回测"
        visible_series = result.get("series", [])
        complete_series = [row for row in visible_series if row.get("complete")]
        z_series = [row for row in visible_series if row.get("z_score") is not None]
        metadata["history"] = {"common_listing_date": COMMON_LIST_DATE,
            "listing_dates": {item["ts_code"]: item.get("list_date") for item in ETF_UNIVERSE},
            "listing_dates_source": "Tushare fund_basic；2026-10-17核实",
            "requested_start": begin, "requested_end": finish,
            "first_observation_date": visible_series[0]["date"] if visible_series else None,
            "first_complete_date": complete_series[0]["date"] if complete_series else None,
            "first_z_date": z_series[0]["date"] if z_series else None,
            "trading_days": len(visible_series), "complete_days": len(complete_series),
            "z_available_days": len(z_series), "warmup_policy": "上市前份额/净值仅作首日相邻数据锚点，不纳入该基金上市后的Z基准"}
        # Trim nested data as well as the top-level benchmark reference.
        metadata["benchmark"] = [x for x in metadata.get("benchmark", []) if begin <= x.get("trade_date", "") <= finish]
        benchmark = [x for x in metadata.get("benchmark", []) if x.get("trade_date", "") <= (result.get("as_of") or finish)]
        fetched_start = metadata.get("fetched_start_date") or metadata.get("requested_start") or metadata.get("requested_start_date")
        fetched_end = metadata.get("fetched_end_date") or metadata.get("requested_end") or metadata.get("requested_end_date")
        range_warnings = []
        if fetched_start and begin < fetched_start:
            range_warnings.append(f"所选开始日 {begin} 早于已抓取范围 {fetched_start}，前段数据未覆盖；请更新所选区间。")
        if fetched_end and finish > fetched_end:
            range_warnings.append(f"所选结束日 {finish} 晚于已抓取范围 {fetched_end}，后段数据未覆盖；请更新所选区间。")
        if masked_navs:
            range_warnings.append(f"已排除 {masked_navs} 条在所选截止日之后公告的净值。")
        result.setdefault("quality", {}).setdefault("warnings", []).extend(range_warnings)
        result.update(metadata=metadata, refresh=status, sources=SOURCE_LINKS, benchmark=benchmark[-1] if benchmark else None)
        result["monitoring"] = monitoring_coverage()
        from .crosschecks import view_crosschecks
        cross = view_crosschecks(min(finish, result.get("as_of") or finish))
        result["crosschecks"] = {**cross, "channels": {key: {**value, "rows": value.get("rows", [])[:crosscheck_limit]} for key, value in cross.get("channels", {}).items()}}
        result["schedule"] = {"enabled": self.auto_update, "times": ["09:00", "18:30", "21:30"], "timezone": "Asia/Shanghai", "requires_running": True}
        return clean_json(result)

    def start_scheduler(self) -> None:
        if self._scheduler_thread is not None and self._scheduler_thread.is_alive():
            return
        self._stop.clear()
        self.auto_update = True
        def run():
            while not self._stop.wait(20):
                self.scheduler_tick(cn_now())
        self._scheduler_thread = threading.Thread(target=run, daemon=True, name="radar-scheduler")
        self._scheduler_thread.start()

    def scheduler_tick(self, now: datetime) -> bool:
        if now.weekday() >= 5:
            return False
        due = [slot for slot in ("09:00", "18:30", "21:30") if slot <= now.strftime("%H:%M")]
        if not due:
            return False
        slot = f"{now.date()} {due[-1]}"
        if slot == self._scheduled_slot:
            return False
        # A busy refresh does not consume the slot: catch up after it finishes,
        # including after resume from sleep. Preserve the user's history start.
        started = self.refresh(start=self.defaults[0], end=now.date().isoformat(), force=False)
        if started:
            self._scheduled_slot = slot
        return started


class RefreshRequest(BaseModel):
    start_date: str | None = None
    end_date: str | None = None


def create_app(store: RadarStore | None = None, auto_update: bool = True, initial_refresh: bool = True) -> FastAPI:
    store = store or RadarStore()
    from .mcp_server import create_mcp_server
    from mcp.server.transport_security import TransportSecuritySettings
    mcp_app = create_mcp_server(store).streamable_http_app(
        stateless_http=True, json_response=True, max_request_body_size=65536,
        # Host validation is performed by the enclosing HTTP middleware below.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))

    @asynccontextmanager
    async def lifespan(app):
        if auto_update:
            store.start_scheduler()
        if initial_refresh:
            store.refresh(*store.defaults, force=False)
        try:
            async with mcp_app.router.lifespan_context(mcp_app):
                yield
        finally:
            store._stop.set()

    app = FastAPI(title="国家队异动雷达", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.radar_store = store
    # Compress JSON (including the background history); SSE is excluded by Starlette.
    app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=4)
    dashboard_cache: OrderedDict = OrderedDict()
    dashboard_lock = threading.Lock()

    allowed_env = os.environ.get("ALLOWED_HOSTS", "127.0.0.1,localhost,testserver,radar.sensen.li").strip()
    allowed_patterns = {h.strip().lower() for h in allowed_env.split(",") if h.strip()}

    @app.middleware("http")
    async def local_origin_guard(request: Request, call_next):
        # Keep local authenticated refresh endpoints out of cross-site requests.
        host = request.headers.get("host", "").split(":")[0].lower()
        is_allowed = (
            "*" in allowed_patterns
            or host in {"127.0.0.1", "localhost", "testserver", "["}
            or host in allowed_patterns
            or any(host.endswith(p[1:]) and host != p[2:] for p in allowed_patterns if p.startswith("*."))
            or any(host.endswith(p) for p in allowed_patterns if p.startswith("."))
        )
        if not is_allowed:
            return JSONResponse({"detail": "此雷达仅供本机或允许域名访问"}, status_code=403)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if origin and origin not in {f"http://{request.headers.get('host')}", f"https://{request.headers.get('host')}"}:
                return JSONResponse({"detail": "不允许跨站更新"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.get("/")
    def index():
        return FileResponse(ROOT / "static" / "index.html")

    @app.get("/api/config")
    def config():
        return {**DEFAULTS, "start_date": store.defaults[0], "end_date": store.defaults[1], "history_start_date": COMMON_LIST_DATE, "today": cn_now().date().isoformat(), "universe": ETF_UNIVERSE}

    @app.get("/api/status")
    def status():
        with store.lock:
            return dict(store.state)

    @app.post("/api/refresh")
    def refresh(body: RefreshRequest):
        with store.lock:
            recent = store.state.get("last_attempt_at")
            if recent and not store.state.get("running"):
                try:
                    if (cn_now() - datetime.fromisoformat(recent)).total_seconds() < 300:
                        raise HTTPException(429, "数据刷新间隔至少5分钟，请稍后再试；读取已有数据不受影响。")
                except (ValueError, TypeError):
                    pass
        try:
            started = store.refresh(body.start_date, body.end_date)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"started": started, **dict(store.state)}

    @app.get("/api/snapshot")
    def snapshot(window: int = Query(20, ge=5, le=120), z_threshold: float = Query(2.0, ge=0.5, le=10), min_flow_yi: float = Query(10, gt=0, le=10000), sync_min: int = Query(3, ge=1, le=7), start_date: str | None = None, end_date: str | None = None):
        try:
            return store.snapshot({"window": window, "z_threshold": z_threshold, "min_flow_yi": min_flow_yi, "sync_min": sync_min}, start_date, end_date)
        except ValueError as exc:
            raise HTTPException(503 if store.data is None else 422, str(exc)) from exc

    @app.get("/api/dashboard")
    def dashboard(scope: str = Query("year", pattern="^(year|all|custom)$"), window: int = Query(20, ge=5, le=120), z_threshold: float = Query(2.0, ge=0.5, le=10), min_flow_yi: float = Query(10, gt=0, le=10000), sync_min: int = Query(3, ge=1, le=7), start_date: str | None = None, end_date: str | None = None):
        """Small initial view, then a lossless columnar history for local scrubbing."""
        try:
            begin, finish = parse_dates(start_date, end_date)
            if scope == "year":
                # Include extra sessions for the first visible day's mini charts.
                begin = max(begin, (date.fromisoformat(finish) - timedelta(days=410)).isoformat())
            params = {"window": window, "z_threshold": z_threshold, "min_flow_yi": min_flow_yi, "sync_min": sync_min}
            with store.lock:
                revision = (id(store.data), json.dumps(store.state, sort_keys=True), store.auto_update)
            key = (revision, begin, finish, tuple(params.values()), scope)
            # Coalesce duplicate requests and bound memory across parameter edits.
            with dashboard_lock:
                cached = dashboard_cache.get(key)
                if cached and time.monotonic() - cached[0] < 30:
                    dashboard_cache.move_to_end(key)
                    return Response(cached[1], media_type="application/json", headers={"Cache-Control": "no-cache"})
                result = store.snapshot(params, begin, finish, crosscheck_limit=None)
                from .dashboard import pack_dashboard
                body = json.dumps(pack_dashboard(result, scope), ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
                dashboard_cache[key] = (time.monotonic(), body)
                dashboard_cache.move_to_end(key)
                while len(dashboard_cache) > 4:
                    dashboard_cache.popitem(last=False)
            return Response(body, media_type="application/json", headers={"Cache-Control": "no-cache"})
        except ValueError as exc:
            raise HTTPException(503 if store.data is None else 422, str(exc)) from exc

    @app.get("/api/export.csv")
    def export(window: int = Query(20, ge=5, le=120), z_threshold: float = Query(2.0, ge=0.5, le=10), min_flow_yi: float = Query(10, gt=0, le=10000), sync_min: int = Query(3, ge=1, le=7), start_date: str | None = None, end_date: str | None = None, review_date: str | None = None):
        try:
            if review_date:
                begin, finish = parse_dates(start_date, end_date)
                review_date = date.fromisoformat(review_date).isoformat()
                if not begin <= review_date <= finish:
                    raise ValueError("回看日期应在导出区间内")
            snapshot = store.snapshot({"window": window, "z_threshold": z_threshold, "min_flow_yi": min_flow_yi, "sync_min": sync_min}, start=start_date, end=end_date)
        except ValueError as exc:
            raise HTTPException(503 if store.data is None else 422, str(exc)) from exc
        output = io.StringIO(newline="")
        columns = ["date", "ts_code", "flow_yi", "delta_share_yi", "total_share_yi", "nav", "close", "z_score", "status", "quality"]
        writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for code, rows in snapshot.get("etf_series", {}).items():
            for row in rows:
                # Match local cursor review: use this disclosure version, then
                # trim dates, rather than hiding later-announced historical NAVs.
                if review_date and row["date"] > review_date:
                    continue
                item = {**row, "ts_code": code}
                if isinstance(item.get("quality"), (list, dict)):
                    item["quality"] = json.dumps(item["quality"], ensure_ascii=False)
                writer.writerow(item)
        return Response("\ufeff" + output.getvalue(), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": 'attachment; filename="etf_flow_radar.csv"'})

    app.router.routes.extend(mcp_app.routes)
    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="国家队异动雷达：本地 Tushare 份额监测")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址，云服务器可设置为 0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--once", action="store_true", help="更新并写入快照/预警 CSV 后退出")
    parser.add_argument("--no-auto-update", action="store_true", help="关闭服务运行期间的定时更新")
    args = parser.parse_args()
    store = RadarStore(args.start_date, args.end_date)
    if args.once:
        store.refresh(*store.defaults, force=True)
        while store.state["running"]:
            store._stop.wait(0.2)
        if store.state["error"]:
            raise SystemExit(store.state["error"])
        snapshot = store.snapshot()
        print(json.dumps({"as_of": snapshot.get("as_of"), "summary": snapshot.get("summary"), "snapshot": str(DATA / "snapshot.json"), "alerts": str(DATA / "alerts.csv")}, ensure_ascii=False))
        return
    import uvicorn
    uvicorn.run(create_app(store, auto_update=not args.no_auto_update), host=args.host, port=args.port, access_log=False)


if __name__ == "__main__":
    main()
