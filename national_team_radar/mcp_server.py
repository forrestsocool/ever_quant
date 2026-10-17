"""Read-only MCP access to the locally cached wide-base ETF radar.

The server uses stdio: an MCP host launches it as a local subprocess. It never
refreshes the Tushare provider, opens a network listener, or serializes the
local credential. All analytical answers come from the same RadarStore snapshot
and cutoff rules as the dashboard.
"""
from __future__ import annotations

import json
import math
from datetime import datetime
import threading
from typing import Any

from mcp.server import MCPServer
from mcp.types import ToolAnnotations
from mcp.server.mcpserver.exceptions import ToolError

from .provider import ETF_UNIVERSE
from .server import DATA, DEFAULTS, ROOT, RadarStore, cn_now


def _validated_config(window: int, z_threshold: float, min_flow_yi: float, sync_min: int) -> dict:
    if not 5 <= window <= 120:
        raise ToolError("window 应在 5 至 120 个交易日之间")
    if not 0.5 <= z_threshold <= 10:
        raise ToolError("z_threshold 应在 0.5 至 10 之间")
    if not 0 < min_flow_yi <= 10_000:
        raise ToolError("min_flow_yi 应大于 0 且不超过 10000 亿元")
    if not 1 <= sync_min <= len(ETF_UNIVERSE):
        raise ToolError(f"sync_min 应在 1 至 {len(ETF_UNIVERSE)} 之间")
    return {"window": window, "z_threshold": z_threshold, "min_flow_yi": min_flow_yi, "sync_min": sync_min}


def _snapshot(store: RadarStore, *, start_date: str | None = None, end_date: str | None = None,
              window: int = 20, z_threshold: float = 2.0, min_flow_yi: float = 10.0,
              sync_min: int = 3) -> dict:
    config = _validated_config(window, z_threshold, min_flow_yi, sync_min)
    try:
        result = store.snapshot(config, start=start_date, end=end_date)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    raw_data = getattr(store, "data", None)
    source_calendar = raw_data.get("calendar") if isinstance(raw_data, dict) else None
    source_latest = max(source_calendar) if source_calendar else (result.get("quality") or {}).get("calendar_latest_session")
    result["_mcp_explicit_end_date"] = end_date
    result["_mcp_source_calendar_latest"] = source_latest
    source_metadata = raw_data.get("metadata", {}) if isinstance(raw_data, dict) else result.get("metadata", {})
    result["_mcp_calendar_coverage_end"] = source_metadata.get("fetched_end_date") or source_metadata.get("requested_end_date") or source_metadata.get("view_end_date")
    return result


def _context(snap: dict) -> dict:
    quality = snap.get("quality") or {}
    coverage = quality.get("coverage") or {}
    summary = snap.get("summary") or {}
    last_complete = snap.get("last_complete") or {}
    metadata = snap.get("metadata") or {}
    refresh = snap.get("refresh") or {}
    view_end = metadata.get("view_end_date")
    calendar_latest = quality.get("calendar_latest_session")
    source_latest = snap.get("_mcp_source_calendar_latest") or calendar_latest
    explicit_end = snap.get("_mcp_explicit_end_date")
    complete_date = last_complete.get("date")
    coverage_end = snap.get("_mcp_calendar_coverage_end") or metadata.get("view_end_date")
    try:
        calendar_expired = (cn_now().date() - datetime.fromisoformat(coverage_end).date()).days > 3
    except (ValueError, TypeError):
        calendar_expired = True
    updated = refresh.get("updated_at")
    try:
        age_hours = (cn_now() - datetime.fromisoformat(updated).astimezone(cn_now().tzinfo)).total_seconds() / 3600
    except (ValueError, TypeError):
        age_hours = None
    return {
        "data_as_of": snap.get("as_of"),
        "calendar_latest": calendar_latest,
        "source_calendar_latest": source_latest,
        "last_complete_date": complete_date,
        "latest_coverage": {"valid": summary.get("valid_count", 0),
                            "total": summary.get("total_count", len(ETF_UNIVERSE)),
                            "missing_codes": coverage.get("missing_codes", []),
                            "quarantined_codes": coverage.get("quarantined_codes", [])},
        "latest_session_complete": bool(complete_date and complete_date == calendar_latest),
        "view_start_date": metadata.get("view_start_date"),
        "view_end_date": view_end,
        "historical_cutoff": bool(explicit_end and source_latest and explicit_end < source_latest),
        "historical_reconstruction": True,
        "refresh_updated_at": refresh.get("updated_at"),
        "refresh_running": bool(refresh.get("running")),
        "refresh_degraded": bool(refresh.get("degraded")),
        "refresh_error": bool(refresh.get("error")),
        "source_health": metadata.get("refresh_health", "unknown"),
        "cache_age_hours": round(age_hours, 2) if age_hours is not None else None,
        "cache_stale": age_hours is None or age_hours > 72 or age_hours < -1,
        "calendar_coverage_end": coverage_end,
        "calendar_expired": calendar_expired,
    }


def _portfolio_fields(row: dict | None) -> dict | None:
    if not row:
        return None
    keys = ("date", "flow_yi", "z_score", "positive_flow_yi", "negative_flow_yi",
            "valid_count", "total_count", "sync_in", "sync_out", "positive_count",
            "negative_count", "status", "complete", "flow_5d_yi", "flow_20d_yi",
            "baseline_days", "baseline_reason", "sync_observation", "quality_codes")
    return {key: row.get(key) for key in keys if key in row}


def _benchmark_on(snap: dict, day: str | None) -> dict | None:
    if not day:
        return None
    for row in reversed((snap.get("metadata") or {}).get("benchmark", [])):
        if row.get("trade_date") == day:
            return {key: row.get(key) for key in ("trade_date", "close", "pct_chg", "source")}
    return None


def _overview(snap: dict) -> dict:
    context = _context(snap)
    complete = snap.get("last_complete")
    return {
        "schema_version": 1,
        "context": context,
        "latest_disclosure": _portfolio_fields(snap.get("summary")),
        "last_complete": _portfolio_fields(complete),
        "csi300_on_last_complete": _benchmark_on(snap, (complete or {}).get("date")),
        "latest_csi300": snap.get("benchmark"),
        "recent_alerts": (snap.get("alerts") or [])[:5],
        "recent_observation_events": (snap.get("observation_events") or [])[:5],
        "warnings": (snap.get("quality") or {}).get("warnings", [])[:10],
        "interpretation": "ETF 份额变化反映一级市场净申赎估算，不能确认交易者身份，也不保证收益。",
    }


def _timing_brief(snap: dict) -> dict:
    context = _context(snap)
    config = snap.get("config") or DEFAULTS
    row = snap.get("last_complete")
    if not row:
        return {"schema_version": 1, "context": context, "decision_state": "insufficient_data",
                "entry_support": None, "exit_pressure": None,
                "reason": "没有全组合可比的完整交易日，不能评估资金异动。"}

    day = row["date"]
    csi300 = _benchmark_on(snap, day)
    current = (context["latest_session_complete"] and not context["historical_cutoff"]
               and not context["refresh_degraded"] and not context["refresh_error"]
               and not context["refresh_running"] and csi300 is not None
               and not context["cache_stale"] and context["source_health"] == "ok"
               and not context["calendar_expired"]
               and row.get("z_score") is not None)
    blocking = []
    if context["cache_stale"]:
        blocking.append("缓存来源时间未知或超过72小时；即使缓存日历自洽，也不能视为最新市场状态")
    if context["calendar_expired"]:
        blocking.append("交易日历查询覆盖已过期；请先将抓取截止日更新至当前日期")
    if context["source_health"] != "ok":
        blocking.append("市场来源健康状态未通过校验")
    if row.get("z_score") is None:
        blocking.append("缺少完整统计基准，Z值不可用，不能评估异动门槛")
    if not context["latest_session_complete"]:
        blocking.append("最新交易日组合数据未完整披露；下述信号属于较早的完整交易日")
    if context["historical_cutoff"]:
        blocking.append("指定了历史截止日；这是事后回看，不能当作当前盘中信号")
    if context["refresh_degraded"] or context["refresh_error"]:
        blocking.append("数据源或刷新状态降级，请先核对来源时间与缺失项")
    if context["refresh_running"]:
        blocking.append("数据正在刷新，结果可能即将改变")
    if csi300 is None:
        blocking.append("同日沪深300指数行情缺失，无法做价格背景核对")

    flow = row.get("flow_yi")
    z = row.get("z_score")
    def directional_watch(direction: str) -> dict:
        inward = direction == "entry"
        signed_flow = flow if inward else (-flow if flow is not None else None)
        signed_z = z if inward else (-z if z is not None else None)
        sync = row.get("sync_in" if inward else "sync_out") or 0
        checks = {
            "complete_portfolio": bool(row.get("complete")),
            "flow_direction_and_amount": signed_flow is not None and signed_flow >= config["min_flow_yi"],
            "z_direction_and_threshold": signed_z is not None and signed_z >= config["z_threshold"],
            "same_direction_etfs": sync >= config["sync_min"],
        }
        triggered = all(checks.values())
        return {
            "direction": "net_creation" if inward else "net_redemption",
            "state": ("triggered_current" if current else "triggered_historical") if triggered else "not_triggered",
            "current_session_usable": bool(triggered and current),
            "checks": checks,
            "observed": {"flow_yi": flow, "z_score": z, "same_direction_etfs": sync},
            "thresholds": {"min_flow_yi": config["min_flow_yi"],
                           "z_threshold": config["z_threshold"], "sync_min": config["sync_min"]},
            "meaning": ("集中净申购值得观察入场时机，需结合价格、风险预算及官方证据"
                        if inward else "集中净赎回值得观察风险收缩或获利了结，无法从本雷达得知个人持仓与成本"),
        }

    entry = directional_watch("entry")
    exit_ = directional_watch("exit")
    nearby = [event for event in (snap.get("observation_events") or []) if event.get("date") == day]
    high_flow_z_without_sync = (flow is not None and z is not None
                                and abs(flow) >= config["min_flow_yi"]
                                and abs(z) >= config["z_threshold"]
                                and flow * z > 0
                                and row.get("status") == "观察")
    divergent = high_flow_z_without_sync or bool(nearby)
    if not current:
        decision_state = "insufficient_data"
    elif entry["current_session_usable"]:
        decision_state = "support_watch"
    elif exit_["current_session_usable"]:
        decision_state = "exit_watch"
    elif divergent:
        decision_state = "divergence"
    else:
        decision_state = "neutral"
    contrasts = []
    index_change = csi300.get("pct_chg") if csi300 else None
    if flow is not None and index_change is not None and flow * index_change < 0:
        contrasts.append("ETF组合资金方向与沪深300同日涨跌相反；价格尚未确认资金方向")
    if high_flow_z_without_sync:
        contrasts.append("组合金额与Z值达到门槛，但同向ETF数量未达标")
    if nearby:
        contrasts.append("多只ETF出现同向异常观察，但组合整体条件尚未达标")
    if (row.get("positive_flow_yi") or 0) >= config["min_flow_yi"] and abs(row.get("negative_flow_yi") or 0) >= config["min_flow_yi"]:
        contrasts.append("组合内部同时存在较大的净申购和净赎回，净额可能掩盖分化")
    evidence = [item for item in (snap.get("evidence") or []) if item.get("date") == day]
    return {
        "schema_version": 1,
        "context": context,
        "decision_state": decision_state,
        "evaluated_session": day,
        "portfolio": _portfolio_fields(row),
        "csi300": csi300,
        "entry_support": entry,
        "exit_pressure": exit_,
        "contrary_observations": contrasts,
        "divergence_note": ("组合金额与Z值达标，但同步只数不足" if high_flow_z_without_sync else
                            "局部ETF同步，但组合整体未达异动条件" if nearby else None),
        "blocking_reasons": blocking,
        "invalidation_conditions": ["后续份额或净值修订", "出现疑似拆分或缺失样本", "下一交易日信号反转", "来源过期或覆盖率下降"],
        "timing": "ETF份额与净值是日频披露，通常盘后或次日可见；本结果不代表盘中即时资金。",
        "same_day_observation_events": nearby,
        "same_day_manual_evidence": evidence,
        "use_limit": "资金异动是观察条件，不是交易指令；不能确认国家队身份、预测获利或替代持仓和风险判断。",
    }


def _quality(snap: dict) -> dict:
    metadata = snap.get("metadata") or {}
    symbols = {}
    for code, item in (metadata.get("symbols") or {}).items():
        symbols[code] = {key: item.get(key) for key in
                         ("mode", "fetched_at", "rows", "share_start", "share_end", "nav_end")}
        for key in ("missing_share_dates", "missing_nav_dates", "non_trading_observation_dates"):
            values = item.get(key) or []
            symbols[code][key] = values[-20:]
            symbols[code][f"{key}_count"] = len(values)
        symbols[code]["error_count"] = len(item.get("errors") or [])
        symbols[code]["warning_count"] = len(item.get("warnings") or [])
    auxiliary = {name: {key: item.get(key) for key in ("mode", "fetched_at")}
                 for name, item in (metadata.get("auxiliary") or {}).items()}
    return {
        "schema_version": 1,
        "context": _context(snap),
        "quality": {**(snap.get("quality") or {}),
                    "warnings": (snap.get("quality") or {}).get("warnings", [])[:20]},
        "symbols": symbols,
        "auxiliary": auxiliary,
        "navs_excluded_after_cutoff": metadata.get("navs_excluded_after_cutoff"),
        "historical_data_policy": metadata.get("historical_data_policy"),
        "sources": snap.get("sources", []),
        "refresh": {**{key: (snap.get("refresh") or {}).get(key) for key in
                       ("running", "stage", "updated_at", "last_attempt_at", "completed_at", "degraded")},
                    "error_present": bool((snap.get("refresh") or {}).get("error"))},
    }


def _speculation_signals(snap: dict, risk_preference: str = "balanced") -> dict:
    brief = _timing_brief(snap)
    entry = brief.get("entry_support") or {}
    row = brief.get("portfolio") or {}
    usable = brief["decision_state"] != "insufficient_data"
    action = "ENTRY_WATCH" if entry.get("current_session_usable") else "NEUTRAL_WATCH" if usable else "INSUFFICIENT_DATA"
    return {
        "schema_version": 2, "context": brief["context"], "evaluated_session": brief.get("evaluated_session"),
        "speculation_action": action, "speculation_score": None, "confidence_level": "UNCALIBRATED",
        "headline": "集中净申购条件成立，进入入场观察" if action == "ENTRY_WATCH" else "当前未形成完整入场条件" if usable else "数据时效或完整性不足，暂停当前择时判断",
        "decision_state": brief["decision_state"], "checks": entry.get("checks", {}),
        "blocking_reasons": brief.get("blocking_reasons", [brief.get("reason")]),
        "observed_metrics": row, "csi300": brief.get("csi300"),
        "contrary_observations": brief.get("contrary_observations", []),
        "bottom_divergence": None,
        "execution_guide": {"risk_preference": risk_preference, "suggested_position": None, "reference_levels": {},
                            "required_inputs": ["真实持仓和成本", "投资期限", "可承担亏损", "流动性与交易约束"]},
        "calibration_status": "规则未经时点可见性回测；不输出胜率、仓位比例或凭空生成的止盈止损价位",
        "interpretation": "单日指数下跌与ETF净申购方向相反，只是同日方向对照，不能证明市场见底、国家队抄底或未来获利。",
    }


def _profit_taking_signals(snap: dict, holding_return_pct: float | None = None,
                           cost_benchmark_level: float | None = None) -> dict:
    for value in (holding_return_pct, cost_benchmark_level):
        if value is not None and not math.isfinite(value):
            raise ToolError("持仓参数须为有限数值")
    if cost_benchmark_level is not None and cost_benchmark_level <= 0:
        raise ToolError("指数成本参考值须大于0")
    brief = _timing_brief(snap)
    exit_ = brief.get("exit_pressure") or {}
    usable = brief["decision_state"] != "insufficient_data"
    action = "EXIT_WATCH" if exit_.get("current_session_usable") else "NEUTRAL_WATCH" if usable else "INSUFFICIENT_DATA"
    return {
        "schema_version": 2, "context": brief["context"], "evaluated_session": brief.get("evaluated_session"),
        "profit_taking_action": action, "decision_state": brief["decision_state"],
        "exit_urgency_score": None, "suggested_reduction_pct": None,
        "headline": "集中净赎回条件成立，检查退出与风险预算" if action == "EXIT_WATCH" else "当前未形成完整赎回异动条件" if usable else "数据时效或完整性不足，暂停当前退出判断",
        "checks": exit_.get("checks", {}), "blocking_reasons": brief.get("blocking_reasons", [brief.get("reason")]),
        "observed_metrics": brief.get("portfolio"), "csi300": brief.get("csi300"), "top_divergence": None,
        "user_supplied_scenario": {"holding_return_pct": holding_return_pct, "cost_benchmark_level": cost_benchmark_level,
                                   "verified": False, "meaning": "用户输入情景，未读取券商真实持仓；指数点位不是具体ETF成本"},
        "execution_guide": {"suggested_reduction_pct": None, "trailing_stop_reference": {}},
        "calibration_status": "未回测校准退出收益或紧迫度；无异动不能推出安心持有",
        "interpretation": "净赎回不能识别国家队卖出；是否获利取决于真实成本、费用和卖出价格，不能从份额推算。",
    }


def _fund_pulse_snapshot(snap: dict) -> dict:
    brief = _timing_brief(snap)
    row = brief.get("portfolio") or {}
    labels = {"support_watch": "集中净申购观察", "exit_watch": "集中净赎回观察", "divergence": "组合条件分化", "neutral": "未达组合异动门槛", "insufficient_data": "数据不足，暂停当前判断"}
    return {"schema_version": 2, "context": brief["context"], "evaluated_session": brief.get("evaluated_session"),
            "decision_state": brief["decision_state"], "pulse_verdict": labels[brief["decision_state"]],
            "pulse_sentiment": "UNASSESSED", "key_recommendation": "先核对截止日、覆盖率、来源状态和逐项门槛，再结合独立披露与个人风险约束。",
            "pulse_core_metrics": row, "csi300": brief.get("csi300"),
            "entry_support": brief.get("entry_support"), "exit_pressure": brief.get("exit_pressure"),
            "blocking_reasons": brief.get("blocking_reasons", []),
            "monitoring_coverage": snap.get("monitoring", {}),
            "independent_evidence": (snap.get("evidence") or [])[:5],
            "contrary_observations": brief.get("contrary_observations", []),
            "interpretation": "资金规模、Z值、同步性来自同一ETF数据族，不能视为三项独立的机构身份证据。"}


def _csi300_correlation_analysis(snap: dict, limit: int = 60) -> dict:
    context = _context(snap)
    benchmark = {row["trade_date"]: row for row in (snap.get("metadata") or {}).get("benchmark", [])}
    series = snap.get("series") or []
    recent = series[-limit:] if len(series) > limit else series
    pairs = []
    bottom_div_dates = []
    top_div_dates = []
    for item in recent:
        d = item["date"]
        flow = item.get("flow_yi")
        z = item.get("z_score")
        idx = benchmark.get(d)
        if idx and flow is not None and idx.get("close") is not None:
            pct = idx.get("pct_chg")
            if pct is None or not item.get("complete"):
                continue
            pairs.append((flow, pct, d, idx.get("close")))
            if flow >= 10.0 and (z or 0) >= 1.5 and pct < -0.5:
                bottom_div_dates.append({"date": d, "flow_yi": flow, "z_score": z, "csi300_pct_chg": pct})
            elif flow <= -10.0 and (z or 0) <= -1.5 and pct > 0.5:
                top_div_dates.append({"date": d, "flow_yi": flow, "z_score": z, "csi300_pct_chg": pct})

    corr = None
    if len(pairs) >= 5:
        flows = [p[0] for p in pairs]
        pcts = [p[1] for p in pairs]
        mean_f = sum(flows) / len(flows)
        mean_p = sum(pcts) / len(pcts)
        num = sum((f - mean_f) * (p - mean_p) for f, p in zip(flows, pcts))
        den_f = sum((f - mean_f) ** 2 for f in flows)
        den_p = sum((p - mean_p) ** 2 for p in pcts)
        if den_f > 0 and den_p > 0:
            corr = round(num / ((den_f * den_p) ** 0.5), 4)

    return {
        "schema_version": 1,
        "context": context,
        "sample_sessions": len(pairs),
        "flow_price_correlation": corr,
        "correlation_interpretation": (
            "样本内资金估算与同日收益呈正相关；不表示因果或预测能力" if corr is not None and corr > 0.2 else
            "样本内资金估算与同日收益呈负相关；无法识别机构身份或政策意图" if corr is not None and corr < -0.2 else
            "样本不足或线性相关较弱；不能据此推断资金动机"
        ),
        "bottom_divergence_events": bottom_div_dates,
        "top_divergence_events": top_div_dates,
        "strategic_takeaway": "仅计算同日样本相关性，未检验下一期收益、交易成本与样本外表现；不据此判断政策底或顶部。",
        "predictive_validation": "not_performed",
    }


def create_mcp_server(store: RadarStore | None = None) -> MCPServer:
    """Build an MCP server over an existing RadarStore or the on-disk cache."""
    radar_store = store if store is not None else RadarStore()
    mcp = MCPServer(
        "national-team-etf-flow-radar",
        title="宽基 ETF 资金异动雷达",
        description="只读本地 ETF 份额、估算净申赎、沪深300行情和量价背离分析，重点为其他 AI 提供择时投机与获利了结决策依据。",
        instructions=(
            "先读取 get_fund_pulse_snapshot 与 get_market_timing_brief。"
            "任何当前入场或退出判断必须通过其完整性、时效及逐项阈值检查。"
            "再用 get_cross_validation 查询官方公告、股东披露与交易辅助线索，用 get_monitoring_coverage 检查接入缺口。"
            "get_speculation_signals/get_profit_taking_signals 是上述规则的兼容视图，均不输出未校准胜率、仓位或目标价。"
            "ETF份额、资金金额和同步性同源，不能当作多项独立身份证据。"
        ),
        version="1.2.0",
    )
    cache_file = DATA / "market_data.json"
    evidence_file = ROOT / "evidence.csv"
    cache_lock = threading.RLock()
    last_market_signature: tuple[int, int] | None = None
    last_evidence_signature: tuple | None = None
    snapshots: dict[tuple, dict] = {}

    def read_snapshot(**kwargs: Any) -> dict:
        nonlocal last_market_signature, last_evidence_signature
        # Dashboard refreshes use atomic replace. A long-lived MCP subprocess
        # must notice each new file; a supplied test/custom store remains owned
        # by its caller and is not reloaded from the global cache.
        if store is None:
            with cache_lock:
                try:
                    stat = cache_file.stat()
                except FileNotFoundError as exc:
                    raise ToolError("本地市场数据缓存不存在；请先在雷达界面更新数据") from exc
                market_signature = (stat.st_mtime_ns, stat.st_size)
                signatures = []
                for path in (evidence_file, DATA / "evidence.csv", DATA / "disclosures.json", DATA / "crosschecks.json"):
                    try:
                        evidence_stat = path.stat()
                        signatures.append((evidence_stat.st_mtime_ns, evidence_stat.st_size))
                    except FileNotFoundError:
                        signatures.append(None)
                evidence_signature = tuple(signatures)
                if market_signature != last_market_signature:
                    with radar_store.lock:
                        radar_store.load()
                    last_market_signature = market_signature
                    snapshots.clear()
                if evidence_signature != last_evidence_signature:
                    last_evidence_signature = evidence_signature
                    snapshots.clear()
                key = tuple(sorted(kwargs.items()))
                if key in snapshots:
                    return snapshots[key]
                result = _snapshot(radar_store, **kwargs)
                if len(snapshots) >= 12:
                    snapshots.pop(next(iter(snapshots)))
                snapshots[key] = result
                return result
        return _snapshot(radar_store, **kwargs)

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False))
    def list_etf_universe() -> dict[str, Any]:
        """List the fixed monitored broad-index ETF universe; available before the first market-data refresh."""
        return {"schema_version": 1, "etfs": ETF_UNIVERSE,
                "identity_policy": "名单表示监测范围，不能由 ETF 名单或份额单独确认交易者身份"}

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False))
    def get_radar_overview(end_date: str | None = None, window: int = 20,
                           z_threshold: float = 2.0, min_flow_yi: float = 10.0,
                           sync_min: int = 3) -> dict[str, Any]:
        """Read the prioritized radar state: latest disclosure, last complete ETF flow, CSI300, alerts, coverage and freshness. End date is an optional historical cutoff (YYYY-MM-DD)."""
        return _overview(read_snapshot(end_date=end_date, window=window,
                                   z_threshold=z_threshold, min_flow_yi=min_flow_yi, sync_min=sync_min))

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False))
    def get_market_timing_brief(end_date: str | None = None, window: int = 20,
                                z_threshold: float = 2.0, min_flow_yi: float = 10.0,
                                sync_min: int = 3) -> dict[str, Any]:
        """Evaluate entry and profit-taking/risk-reduction WATCH conditions separately. Returns exact threshold checks, CSI300 context and blockers; historical or incomplete data never masquerades as a current signal. No return guarantee."""
        return _timing_brief(read_snapshot(end_date=end_date, window=window,
                                       z_threshold=z_threshold, min_flow_yi=min_flow_yi, sync_min=sync_min))

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False))
    def get_daily_series(start_date: str | None = None, end_date: str | None = None,
                         limit: int = 60, offset: int = 0, window: int = 20, z_threshold: float = 2.0,
                         min_flow_yi: float = 10.0, sync_min: int = 3) -> dict[str, Any]:
        """Read daily portfolio flow plus same-date CSI300. Max 120 rows; offset skips that many newest rows to page backward. Date bounds and thresholds are configurable."""
        if not 1 <= limit <= 120 or not 0 <= offset <= 10_000:
            raise ToolError("limit 应在 1 至 120，offset 应在 0 至 10000")
        snap = read_snapshot(start_date=start_date, end_date=end_date,
                         window=window, z_threshold=z_threshold, min_flow_yi=min_flow_yi, sync_min=sync_min)
        benchmark = {row["trade_date"]: row for row in (snap.get("metadata") or {}).get("benchmark", [])}
        series = snap.get("series") or []
        end_at = max(0, len(series) - offset)
        begin_at = max(0, end_at - limit)
        rows = []
        for item in series[begin_at:end_at]:
            index = benchmark.get(item["date"]) or {}
            rows.append({**_portfolio_fields(item), "csi300_close": index.get("close"),
                         "csi300_pct_chg": index.get("pct_chg")})
        return {"schema_version": 1, "context": _context(snap), "rows": rows,
                "returned": len(rows), "total_in_selected_range": len(series),
                "next_offset": offset + len(rows) if begin_at > 0 else None,
                "order": "oldest_to_newest", "units": {"flow_yi": "亿元", "csi300_close": "指数点"}}

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False))
    def get_etf_detail(ts_code: str, start_date: str | None = None, end_date: str | None = None,
                       limit: int = 60, offset: int = 0, window: int = 20, z_threshold: float = 2.0,
                       min_flow_yi: float = 10.0, sync_min: int = 3) -> dict[str, Any]:
        """Read one ETF's daily shares, prior-NAV flow, Z-score and data flags. TS code such as 510300.SH; max 120 rows; offset pages backward."""
        code = ts_code.strip().upper()
        if code not in {item["ts_code"] for item in ETF_UNIVERSE}:
            raise ToolError("ETF 不在固定监控组合中；先读取 radar://universe")
        if not 1 <= limit <= 120 or not 0 <= offset <= 10_000:
            raise ToolError("limit 应在 1 至 120，offset 应在 0 至 10000")
        snap = read_snapshot(start_date=start_date, end_date=end_date,
                         window=window, z_threshold=z_threshold, min_flow_yi=min_flow_yi, sync_min=sync_min)
        history = (snap.get("etf_series") or {}).get(code, [])
        latest = next((item for item in snap.get("etfs", []) if item.get("ts_code") == code), None)
        end_at = max(0, len(history) - offset)
        begin_at = max(0, end_at - limit)
        rows = history[begin_at:end_at]
        return {"schema_version": 1, "context": _context(snap), "ts_code": code,
                "definition": next(item for item in ETF_UNIVERSE if item["ts_code"] == code),
                "latest": latest, "rows": rows, "returned": len(rows),
                "next_offset": offset + len(rows) if begin_at > 0 else None,
                "total_in_selected_range": len(history), "order": "oldest_to_newest",
                "units": (snap.get("method") or {}).get("units", {})}

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False))
    def get_historical_events(start_date: str | None = None, end_date: str | None = None,
                              limit: int = 50, offset: int = 0, window: int = 20, z_threshold: float = 2.0,
                              min_flow_yi: float = 10.0, sync_min: int = 3) -> dict[str, Any]:
        """Read alerts, ETF synchrony observations and labelled manual evidence. Newest first; max 120 rows; offset pages older events."""
        if not 1 <= limit <= 120 or not 0 <= offset <= 10_000:
            raise ToolError("limit 应在 1 至 120，offset 应在 0 至 10000")
        snap = read_snapshot(start_date=start_date, end_date=end_date,
                         window=window, z_threshold=z_threshold, min_flow_yi=min_flow_yi, sync_min=sync_min)
        begin = (snap.get("metadata") or {}).get("view_start_date")
        events = ([{"event_type": "portfolio_alert", **row} for row in snap.get("alerts", [])]
                  + [{"event_type": "synchrony_observation", **row} for row in snap.get("observation_events", [])]
                  + [{"event_type": "manual_evidence", **row} for row in snap.get("evidence", [])
                     if begin is None or row.get("date", "") >= begin])
        events.sort(key=lambda item: item.get("date", ""), reverse=True)
        selected = events[offset:offset + limit]
        return {"schema_version": 1, "context": _context(snap), "events": selected,
                "returned": len(selected), "total_in_selected_range": len(events),
                "next_offset": offset + len(selected) if offset + len(selected) < len(events) else None,
                "manual_evidence_policy": "人工录入且未自动核验，不参与资金信号评分"}

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False))
    def get_monitoring_coverage() -> dict[str, Any]:
        """Read the original monitoring-method checklist: connected, partially covered, unavailable and unimplemented channels, scope and source status. Never assume coverage from a tool name."""
        snap = read_snapshot()
        return {"schema_version": 1, "context": _context(snap), "monitoring": snap.get("monitoring", {})}

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False))
    def get_cross_validation(channel: str = "official", end_date: str | None = None,
                             limit: int = 30, offset: int = 0) -> dict[str, Any]:
        """Read independent official disclosures or auxiliary observations. channel: official, holders, dragon_tiger, block_trades, large_orders. Official publication dates and first-seen times are distinct; max120 rows. No automatic identity score."""
        if not 1 <= limit <= 120 or not 0 <= offset <= 10000:
            raise ToolError("limit应为1至120，offset应为0至10000")
        snap = read_snapshot(end_date=end_date)
        if channel == "official":
            rows = [row for row in snap.get("evidence", []) if row.get("verification") == "official_source"]
            status = {"sources": (snap.get("monitoring") or {}).get("official_sources", {})}
        else:
            from .crosschecks import DEFINITIONS, view_crosschecks
            if channel not in DEFINITIONS:
                raise ToolError("未知渠道，请先调用get_monitoring_coverage")
            item = view_crosschecks(min(end_date or snap["as_of"], snap["as_of"])).get("channels", {}).get(channel, {})
            rows, status = item.get("rows", []), {k: v for k, v in item.items() if k != "rows"}
        return {"schema_version": 1, "context": _context(snap), "channel": channel, "status": status,
                "rows": rows[offset:offset + limit], "total": len(rows),
                "next_offset": offset + limit if offset + limit < len(rows) else None,
                "affects_score": False, "identity_confirmed": False,
                "historical_visibility": "按公告日/交易日过滤；首次发现时间单列，未重建历史时点完整可见性"}

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False))
    def get_data_quality(end_date: str | None = None) -> dict[str, Any]:
        """Inspect coverage, missing/quarantined ETF rows, source modes/timestamps, stale refresh state, and source documentation. No credentials are returned."""
        return _quality(read_snapshot(end_date=end_date))

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False))
    def get_methodology(end_date: str | None = None, window: int = 20,
                        z_threshold: float = 2.0, min_flow_yi: float = 10.0,
                        sync_min: int = 3) -> dict[str, Any]:
        """Read exact share-to-flow formula, units, rolling baseline, split quarantine, synchrony criteria, limitations, and current thresholds."""
        snap = read_snapshot(end_date=end_date, window=window,
                         z_threshold=z_threshold, min_flow_yi=min_flow_yi, sync_min=sync_min)
        return {"schema_version": 1, "context": _context(snap), "config": snap.get("config"),
                "method": snap.get("method"), "sources": snap.get("sources", [])}

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False))
    def get_speculation_signals(
        end_date: str | None = None,
        risk_preference: str = "balanced",
        window: int = 20,
        z_threshold: float = 2.0,
        min_flow_yi: float = 10.0,
        sync_min: int = 3,
    ) -> dict[str, Any]:
        """入场观察兼容接口：与主择时摘要共用质量门槛；不产出未经回测的评分、胜率、仓位或价格目标。"""
        if risk_preference not in {"aggressive", "balanced", "conservative"}:
            risk_preference = "balanced"
        snap = read_snapshot(end_date=end_date, window=window, z_threshold=z_threshold, min_flow_yi=min_flow_yi, sync_min=sync_min)
        return _speculation_signals(snap, risk_preference=risk_preference)

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False))
    def get_profit_taking_signals(
        end_date: str | None = None,
        holding_return_pct: float | None = None,
        cost_benchmark_level: float | None = None,
        window: int = 20,
        z_threshold: float = 2.0,
        min_flow_yi: float = 10.0,
        sync_min: int = 3,
    ) -> dict[str, Any]:
        """退出观察兼容接口：明确赎回门槛、时效阻断与用户提供情景；未知实际持仓时不推算盈利或减仓比例。"""
        snap = read_snapshot(end_date=end_date, window=window, z_threshold=z_threshold, min_flow_yi=min_flow_yi, sync_min=sync_min)
        return _profit_taking_signals(snap, holding_return_pct=holding_return_pct, cost_benchmark_level=cost_benchmark_level)

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False))
    def get_fund_pulse_snapshot(
        end_date: str | None = None,
        window: int = 20,
        z_threshold: float = 2.0,
        min_flow_yi: float = 10.0,
        sync_min: int = 3,
    ) -> dict[str, Any]:
        """【宽基资金脉搏高光全景快照】突出重点，一站式返回最新资金脉搏定性、沪深300对比、核心量化指标与对其他 AI 的最高优先级操作指引。"""
        snap = read_snapshot(end_date=end_date, window=window, z_threshold=z_threshold, min_flow_yi=min_flow_yi, sync_min=sync_min)
        return _fund_pulse_snapshot(snap)

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False))
    def get_csi300_correlation_analysis(
        limit: int = 60,
        end_date: str | None = None,
        window: int = 20,
    ) -> dict[str, Any]:
        """【沪深300指数与资金脉搏协同分析】计算历史区间资金流与沪深300指数的滚动相关系数、量价底背离/顶背离统计及中短期行情启示。"""
        if not 5 <= limit <= 300:
            raise ToolError("limit 应在 5 至 300 之间")
        snap = read_snapshot(end_date=end_date, window=window)
        return _csi300_correlation_analysis(snap, limit=limit)

    @mcp.resource("radar://overview")
    def overview_resource() -> str:
        """Current prioritized radar overview, including disclosure completeness."""
        return json.dumps(_overview(read_snapshot()), ensure_ascii=False, allow_nan=False)

    @mcp.resource("radar://timing")
    def timing_resource() -> str:
        """Current entry and exit watch conditions with freshness blockers."""
        return json.dumps(_timing_brief(read_snapshot()), ensure_ascii=False, allow_nan=False)

    @mcp.resource("radar://pulse")
    def pulse_resource() -> str:
        """High-priority broad fund pulse snapshot."""
        return json.dumps(_fund_pulse_snapshot(read_snapshot()), ensure_ascii=False, allow_nan=False)

    @mcp.resource("radar://speculation")
    def speculation_resource() -> str:
        """Latest entry watch with threshold checks and freshness blockers."""
        return json.dumps(_speculation_signals(read_snapshot()), ensure_ascii=False, allow_nan=False)

    @mcp.resource("radar://profit-taking")
    def profit_taking_resource() -> str:
        """Latest profit-taking and risk reduction guidance."""
        return json.dumps(_profit_taking_signals(read_snapshot()), ensure_ascii=False, allow_nan=False)

    @mcp.resource("radar://methodology")
    def methodology_resource() -> str:
        """Calculation and identity limitations from the current analyzed snapshot."""
        snap = read_snapshot()
        return json.dumps({"config": snap.get("config"), "method": snap.get("method")},
                          ensure_ascii=False, allow_nan=False)

    @mcp.resource("radar://quality")
    def quality_resource() -> str:
        """Current per-source freshness and data-quality status."""
        return json.dumps(_quality(read_snapshot()), ensure_ascii=False, allow_nan=False)

    @mcp.resource("radar://universe")
    def universe_resource() -> str:
        """The fixed, explicit list of monitored broad-index ETFs."""
        return json.dumps(ETF_UNIVERSE, ensure_ascii=False, allow_nan=False)

    @mcp.resource("radar://etf/{ts_code}")
    def etf_resource(ts_code: str) -> str:
        """Latest detailed row for a monitored ETF."""
        code = ts_code.strip().upper()
        if code not in {item["ts_code"] for item in ETF_UNIVERSE}:
            raise ToolError("ETF 不在固定监控组合中")
        snap = read_snapshot()
        latest = next((item for item in snap.get("etfs", []) if item.get("ts_code") == code), None)
        return json.dumps({"context": _context(snap), "ts_code": code, "latest": latest},
                          ensure_ascii=False, allow_nan=False)

    @mcp.prompt()
    def timing_speculation_strategy() -> str:
        """Evidence-first timing review, with explicit freshness and coverage blockers."""
        return "先调用 get_market_timing_brief 和 get_monitoring_coverage，再查询 get_cross_validation。报告截止日、数据缺口、逐项门槛、独立披露及反向证据；数据不可用时暂停当前判断。不得把未回测规则当作胜率、给出固定仓位或凭空推算目标价。"

    @mcp.prompt()
    def profit_taking_strategy(user_return_pct: float | None = None) -> str:
        """Review exit observations using an optional, explicitly supplied holding scenario."""
        return f"先核对 get_market_timing_brief 与 get_profit_taking_signals 的阻断项，再用 get_cross_validation 复核。用户提供浮盈情景：{user_return_pct}（None表示未知，未向券商核验）。报告赎回条件、缺失信息与反证，不编造个人盈利、减仓比例或安全持有结论。"

    return mcp


def main() -> None:
    """Run the local-only MCP stdio process for an MCP host."""
    create_mcp_server().run(transport="stdio")


if __name__ == "__main__":
    main()
