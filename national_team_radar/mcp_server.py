"""Read-only MCP access to the locally cached wide-base ETF radar.

The server uses stdio: an MCP host launches it as a local subprocess. It never
refreshes the Tushare provider, opens a network listener, or serializes the
local credential. All analytical answers come from the same RadarStore snapshot
and cutoff rules as the dashboard.
"""
from __future__ import annotations

import json
import threading
from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from .provider import ETF_UNIVERSE
from .server import DATA, DEFAULTS, ROOT, RadarStore


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
               and not context["refresh_running"] and csi300 is not None)
    blocking = []
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
    context = _context(snap)
    config = snap.get("config") or DEFAULTS
    row = snap.get("last_complete")
    if not row:
        return {
            "schema_version": 1,
            "context": context,
            "speculation_action": "INSUFFICIENT_DATA",
            "speculation_score": 0.0,
            "confidence_level": "LOW",
            "headline": "缺少可比的完整交易日数据",
            "bottom_divergence": False,
            "timing_rationale": "当前区间没有全组合可比的交易日，无法评估宽基资金异动与择时信号。",
            "execution_guide": {},
            "invalidation_triggers": [],
            "interpretation": "数据不足，暂不产出投机信号。",
        }

    day = row["date"]
    csi300 = _benchmark_on(snap, day)
    csi300_close = csi300.get("close") if csi300 else None
    csi300_pct_chg = csi300.get("pct_chg") if csi300 else None

    flow = row.get("flow_yi")
    z = row.get("z_score")
    sync_in = row.get("sync_in") or 0
    sync_out = row.get("sync_out") or 0
    flow_5d = row.get("flow_5d_yi")
    flow_20d = row.get("flow_20d_yi")

    bottom_divergence = bool(
        flow is not None and flow >= config["min_flow_yi"] and z is not None and z >= 1.5
        and csi300_pct_chg is not None and csi300_pct_chg < 0
    )

    score = 50.0
    factors = []

    if flow is not None:
        if flow >= 200.0:
            score += 25.0
            factors.append(f"单日资金呈特大级净申购（+{flow:.1f} 亿元）")
        elif flow >= 100.0:
            score += 20.0
            factors.append(f"单日资金呈巨额净申购（+{flow:.1f} 亿元）")
        elif flow >= 50.0:
            score += 15.0
            factors.append(f"单日资金呈显著净申购（+{flow:.1f} 亿元）")
        elif flow >= 20.0:
            score += 10.0
            factors.append(f"单日资金呈中度净申购（+{flow:.1f} 亿元）")
        elif flow >= config["min_flow_yi"]:
            score += 5.0
            factors.append(f"单日资金达到异动门槛（+{flow:.1f} 亿元）")
        elif flow <= -100.0:
            score -= 35.0
            factors.append(f"单日资金出现巨额净赎回（{flow:.1f} 亿元）")
        elif flow <= -20.0:
            score -= 20.0
            factors.append(f"单日资金呈显著净赎回（{flow:.1f} 亿元）")
        elif flow < 0:
            score -= 10.0
            factors.append(f"单日资金呈净流出态势（{flow:.1f} 亿元）")

    if z is not None:
        if z >= 5.0:
            score += 20.0
            factors.append(f"统计异常度极高（Z = +{z:.2f}）")
        elif z >= 3.0:
            score += 15.0
            factors.append(f"统计异常度显著（Z = +{z:.2f}）")
        elif z >= config["z_threshold"]:
            score += 10.0
            factors.append(f"统计异常度达到阈值（Z = +{z:.2f}）")
        elif z >= 1.0:
            score += 5.0
        elif z <= -3.0:
            score -= 20.0
            factors.append(f"负向统计异常显著（Z = {z:.2f}）")
        elif z <= -config["z_threshold"]:
            score -= 10.0
            factors.append(f"负向统计异常度达标（Z = {z:.2f}）")

    if sync_in >= 4:
        score += 15.0
        factors.append(f"多达 {sync_in} 只核心宽基同向同步大额净申购")
    elif sync_in >= config["sync_min"]:
        score += 10.0
        factors.append(f"{sync_in} 只核心宽基同向同步大额净申购")
    elif sync_in >= 2:
        score += 5.0
        factors.append(f"{sync_in} 只核心宽基出现局部同步申购")

    if bottom_divergence:
        score += 15.0
        factors.append(f"量价底背离：指数单日下跌 {csi300_pct_chg:.2f}%，资金逆势巨额抄底托底")

    if flow_5d is not None:
        if flow_5d >= 100.0:
            score += 10.0
            factors.append(f"近 5 日累计净流入达 +{flow_5d:.1f} 亿元，动量强劲")
        elif flow_5d >= 30.0:
            score += 5.0
        elif flow_5d <= -50.0:
            score -= 15.0
            factors.append(f"近 5 日累计净流出达 {flow_5d:.1f} 亿元，流动性承压")

    score = max(0.0, min(100.0, round(score, 1)))

    if score >= 80.0:
        action = "STRONG_SPECULATIVE_BUY"
        headline = "极强资金托底支撑，胜率高，建议积极做多 / 逢低重仓投机"
        confidence = "HIGH"
    elif score >= 65.0:
        action = "SPECULATIVE_BUY"
        headline = "资金显著异常净流入，建议逢低买入试探 / 积极博弈反弹"
        confidence = "HIGH" if bottom_divergence else "MEDIUM"
    elif score >= 55.0:
        action = "ACCUMULATE"
        headline = "资金呈现温和净申购，建议分批吸筹 / 定投式逢低配置"
        confidence = "MEDIUM"
    elif score <= 35.0:
        action = "AVOID"
        headline = "资金呈现净流出态势，规避多头投机，防范阴跌风险"
        confidence = "HIGH" if sync_out >= 2 else "MEDIUM"
    else:
        action = "NEUTRAL_WAIT"
        headline = "多空资金相对均衡，建议多看少动、等待明确方向信号"
        confidence = "LOW"

    pref = risk_preference.lower()
    if action == "STRONG_SPECULATIVE_BUY":
        pos_pct = 70 if pref == "aggressive" else (50 if pref == "balanced" else 30)
        pos_text = f"建议 {pos_pct}% 仓位积极参与，分两批在盘中震荡回调时建仓"
    elif action == "SPECULATIVE_BUY":
        pos_pct = 50 if pref == "aggressive" else (30 if pref == "balanced" else 20)
        pos_text = f"建议 {pos_pct}% 仓位试探性建仓，严格设好止损"
    elif action == "ACCUMULATE":
        pos_pct = 30 if pref == "aggressive" else (20 if pref == "balanced" else 10)
        pos_text = f"建议 {pos_pct}% 底仓分批低吸，不宜追高"
    elif action == "AVOID":
        pos_text = "建议空仓或极轻仓（< 10%），等待流动性企稳"
    else:
        pos_text = "建议保持 10% - 20% 观望仓位，暂不加大赌注"

    etf_rankings = []
    for item in snap.get("etfs", []):
        code = item.get("ts_code")
        series_item = next((r for r in reversed((snap.get("etf_series") or {}).get(code, [])) if r.get("date") == day), None)
        if series_item:
            etf_rankings.append({
                "ts_code": code,
                "name": item.get("name"),
                "index": item.get("index"),
                "flow_yi": series_item.get("flow_yi"),
                "z_score": series_item.get("z_score"),
                "delta_share_yi": series_item.get("delta_share_yi"),
                "share_change_pct": series_item.get("share_change_pct"),
                "close": series_item.get("close"),
            })
    etf_rankings.sort(key=lambda x: (x.get("flow_yi") is not None, x.get("flow_yi") or -9999), reverse=True)
    top_targets = etf_rankings[:3] if etf_rankings else []

    levels = {}
    if csi300_close:
        levels = {
            "csi300_evaluated_close": csi300_close,
            "stop_loss_benchmark_level": round(csi300_close * 0.982, 2),
            "take_profit_target_1": round(csi300_close * 1.03, 2),
            "take_profit_target_2": round(csi300_close * 1.065, 2),
        }

    return {
        "schema_version": 1,
        "context": context,
        "evaluated_session": day,
        "speculation_action": action,
        "speculation_score": score,
        "confidence_level": confidence,
        "headline": headline,
        "bottom_divergence": bottom_divergence,
        "observed_metrics": {
            "flow_yi": flow,
            "z_score": z,
            "sync_in": sync_in,
            "sync_out": sync_out,
            "flow_5d_yi": flow_5d,
            "flow_20d_yi": flow_20d,
            "csi300_close": csi300_close,
            "csi300_pct_chg": csi300_pct_chg,
        },
        "score_factors": factors,
        "execution_guide": {
            "risk_preference": risk_preference,
            "suggested_position": pos_text,
            "top_target_etfs": top_targets,
            "reference_levels": levels,
        },
        "invalidation_triggers": [
            "次日组合资金转为净赎回（金额 > 15 亿元且 Z < -1.5）",
            "沪深300收盘有效跌破止损参考线",
            "同向大额申购的龙头 ETF 出现突发份额撤回或净值下修",
        ],
        "interpretation": "ETF 份额变化反映一级市场估算净申赎。量价底背离大额申购为统计强势信号，但并不保证未来 100% 获利，请严格执行止损纪律。",
    }


def _profit_taking_signals(
    snap: dict,
    holding_return_pct: float | None = None,
    cost_benchmark_level: float | None = None,
) -> dict:
    context = _context(snap)
    config = snap.get("config") or DEFAULTS
    row = snap.get("last_complete")
    if not row:
        return {
            "schema_version": 1,
            "context": context,
            "profit_taking_action": "INSUFFICIENT_DATA",
            "exit_urgency_score": 0.0,
            "suggested_reduction_pct": 0,
            "headline": "缺少可比的完整交易日数据",
            "top_divergence": False,
            "observed_metrics": {},
            "score_factors": [],
            "execution_guide": {},
            "interpretation": "当前区间没有全组合可比的交易日，无法评估宽基资金赎回压力与了结信号。",
        }

    day = row["date"]
    csi300 = _benchmark_on(snap, day)
    csi300_close = csi300.get("close") if csi300 else None
    csi300_pct_chg = csi300.get("pct_chg") if csi300 else None

    flow = row.get("flow_yi")
    z = row.get("z_score")
    sync_out = row.get("sync_out") or 0
    sync_in = row.get("sync_in") or 0
    flow_5d = row.get("flow_5d_yi")

    top_divergence = bool(
        flow is not None and flow <= -config["min_flow_yi"] and z is not None and z <= -1.5
        and csi300_pct_chg is not None and csi300_pct_chg > 0.3
    )

    urgency = 15.0
    factors = []

    if flow is not None:
        if flow <= -150.0:
            urgency += 40.0
            factors.append(f"单日资金遭遇极端大额净赎回（{flow:.1f} 亿元）")
        elif flow <= -80.0:
            urgency += 30.0
            factors.append(f"单日资金遭遇巨额净赎回（{flow:.1f} 亿元）")
        elif flow <= -30.0:
            urgency += 20.0
            factors.append(f"单日资金出现显著净赎回（{flow:.1f} 亿元）")
        elif flow <= -config["min_flow_yi"]:
            urgency += 12.0
            factors.append(f"单日资金赎回达到异动阈值（{flow:.1f} 亿元）")
        elif flow > 50.0:
            urgency -= 15.0
            factors.append(f"单日资金大额净申购（+{flow:.1f} 亿元），流动性支撑充足")
        elif flow > 0:
            urgency -= 10.0
            factors.append(f"单日资金保持净流入（+{flow:.1f} 亿元）")

    if z is not None:
        if z <= -4.0:
            urgency += 25.0
            factors.append(f"负向偏离度极其反常（Z = {z:.2f}）")
        elif z <= -config["z_threshold"]:
            urgency += 15.0
            factors.append(f"负向偏离度达到警戒阈值（Z = {z:.2f}）")
        elif z <= -1.0:
            urgency += 8.0
        elif z >= 2.0:
            urgency -= 15.0

    if sync_out >= 3:
        urgency += 20.0
        factors.append(f"出现 {sync_out} 只核心宽基跨品种同步净赎回共振")
    elif sync_out >= 2:
        urgency += 10.0
        factors.append(f"出现 {sync_out} 只核心宽基同步赎回迹象")

    if top_divergence:
        urgency += 20.0
        factors.append(f"量价顶背离：指数上涨 {csi300_pct_chg:.2f}%，资金却大幅撤离逢高派发")

    if flow_5d is not None and flow_5d <= -50.0:
        urgency += 15.0
        factors.append(f"近 5 日累计净赎回达 {flow_5d:.1f} 亿元，资金撤离呈持续性")

    if holding_return_pct is not None:
        if holding_return_pct >= 25.0 and (flow is not None and flow < 0):
            urgency += 20.0
            factors.append(f"当前持仓丰厚浮盈（+{holding_return_pct:.1f}%），遇资金净流出应优先落袋为安")
        elif holding_return_pct >= 10.0 and (flow is not None and flow < 0):
            urgency += 10.0
            factors.append(f"当前持仓已有浮盈（+{holding_return_pct:.1f}%），建议防守保护利润")

    urgency = max(0.0, min(100.0, round(urgency, 1)))

    if urgency >= 75.0:
        action = "STRONG_TAKE_PROFIT"
        headline = "大额资金集中撤退，赎回压力极大，强烈建议果断获利了结 / 大幅减仓锁定利润"
        reduction_pct = 70
    elif urgency >= 50.0:
        action = "PARTIAL_TAKE_PROFIT"
        headline = "资金出现明显流出与背离压力，建议分批落袋为安、减仓 30% - 50% 利润"
        reduction_pct = 40
    elif urgency >= 35.0:
        action = "TRAILING_STOP"
        headline = "短期存在一定分化，建议启动移动止盈，上移出局保护线，不盲目加仓"
        reduction_pct = 15
    else:
        action = "HOLD_SAFE"
        headline = "宽基资金未见大额撤退抛压，流动性良好，可安心持有"
        reduction_pct = 0

    redemption_etfs = []
    for item in snap.get("etfs", []):
        code = item.get("ts_code")
        series_item = next((r for r in reversed((snap.get("etf_series") or {}).get(code, [])) if r.get("date") == day), None)
        if series_item and (series_item.get("flow_yi") or 0) < 0:
            redemption_etfs.append({
                "ts_code": code,
                "name": item.get("name"),
                "flow_yi": series_item.get("flow_yi"),
                "z_score": series_item.get("z_score"),
                "share_change_pct": series_item.get("share_change_pct"),
            })
    redemption_etfs.sort(key=lambda x: x.get("flow_yi") or 0)

    trailing_guide = {}
    if csi300_close:
        trailing_guide = {
            "csi300_close": csi300_close,
            "protective_stop_level": round(csi300_close * 0.99, 2),
            "hard_exit_level": round(csi300_close * 0.975, 2),
        }

    return {
        "schema_version": 1,
        "context": context,
        "evaluated_session": day,
        "profit_taking_action": action,
        "exit_urgency_score": urgency,
        "suggested_reduction_pct": reduction_pct,
        "headline": headline,
        "top_divergence": top_divergence,
        "observed_metrics": {
            "flow_yi": flow,
            "z_score": z,
            "sync_out": sync_out,
            "flow_5d_yi": flow_5d,
            "csi300_close": csi300_close,
            "csi300_pct_chg": csi300_pct_chg,
            "user_holding_return_pct": holding_return_pct,
            "user_cost_benchmark_level": cost_benchmark_level,
        },
        "score_factors": factors,
        "execution_guide": {
            "action_guidance": f"建议减仓约 {reduction_pct}% 锁定收益" if reduction_pct > 0 else "维持当前持仓，无需主动减仓",
            "trailing_stop_reference": trailing_guide,
            "high_redemption_pressure_etfs": redemption_etfs,
        },
        "interpretation": "获利了结信号着重防范大机构/国家队借行情走强实施大额赎回派发。投资决策需结合自身真实买入成本与风险承受力。",
    }


def _fund_pulse_snapshot(snap: dict) -> dict:
    context = _context(snap)
    row = snap.get("last_complete") or {}
    day = row.get("date")
    csi300 = _benchmark_on(snap, day)
    spec = _speculation_signals(snap)
    exit_sig = _profit_taking_signals(snap)
    flow = row.get("flow_yi")
    z = row.get("z_score")

    if flow is not None and flow >= 100.0:
        verdict = "极强资金托底"
        sentiment = "BULLISH_SUPPORT"
    elif flow is not None and flow >= 20.0:
        verdict = "大额资金净流入"
        sentiment = "BULLISH"
    elif flow is not None and flow <= -50.0:
        verdict = "大额资金净赎回预警"
        sentiment = "BEARISH"
    elif flow is not None and flow < 0:
        verdict = "偏弱资金净流出"
        sentiment = "BEARISH_LEANING"
    else:
        verdict = "多空博弈均衡"
        sentiment = "NEUTRAL"

    key_recommendation = (
        f"【核心建议】{day} 宽基资金脉搏表现为「{verdict}」（当日净申赎 {flow:+.2f} 亿元，Z-Score {z:+.2f}）。"
        f"择时信号：{spec.get('headline')}。"
        f"止盈防守：{exit_sig.get('headline')}。"
    ) if flow is not None and z is not None else "【核心建议】数据尚待进一步更新，保持观察。"

    return {
        "schema_version": 1,
        "context": context,
        "evaluated_session": day,
        "pulse_verdict": verdict,
        "pulse_sentiment": sentiment,
        "key_recommendation": key_recommendation,
        "pulse_core_metrics": {
            "flow_yi": flow,
            "z_score": z,
            "sync_in": row.get("sync_in", 0),
            "sync_out": row.get("sync_out", 0),
            "flow_5d_yi": row.get("flow_5d_yi"),
            "flow_20d_yi": row.get("flow_20d_yi"),
            "csi300_close": csi300.get("close") if csi300 else None,
            "csi300_pct_chg": csi300.get("pct_chg") if csi300 else None,
        },
        "speculation_summary": {
            "action": spec.get("speculation_action"),
            "score": spec.get("speculation_score"),
            "confidence": spec.get("confidence_level"),
            "suggested_position": spec.get("execution_guide", {}).get("suggested_position"),
        },
        "profit_taking_summary": {
            "action": exit_sig.get("profit_taking_action"),
            "urgency_score": exit_sig.get("exit_urgency_score"),
            "suggested_reduction_pct": exit_sig.get("suggested_reduction_pct"),
        },
        "divergence_signal": (
            "底背离：指数下跌但大额资金进场抄底" if spec.get("bottom_divergence") else
            "顶背离：指数冲高但大额资金减持撤退" if exit_sig.get("top_divergence") else
            "无显著量价背离"
        ),
    }


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
            pct = idx.get("pct_chg") or 0.0
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
            "正相关：资金随市场上涨申购、下跌赎回（顺势型）" if corr is not None and corr > 0.2 else
            "负相关：大额资金经常逆市托底抄底或逢高获利了结（逆市平准型）" if corr is not None and corr < -0.2 else
            "弱相关/低线性相关：资金进出具有较强的主动独立性"
        ),
        "bottom_divergence_events": bottom_div_dates,
        "top_divergence_events": top_div_dates,
        "strategic_takeaway": (
            f"在过去 {len(pairs)} 个交易日中，发现了 {len(bottom_div_dates)} 次典型底背离（指数大跌大额逆势抄底）"
            f"以及 {len(top_div_dates)} 次典型顶背离。底背离日通常对应着中短期的强力政策底或市场情绪底。"
        ),
    }


def create_mcp_server(store: RadarStore | None = None) -> MCPServer:
    """Build an MCP server over an existing RadarStore or the on-disk cache."""
    radar_store = store if store is not None else RadarStore()
    mcp = MCPServer(
        "national-team-etf-flow-radar",
        title="宽基 ETF 资金异动雷达",
        description="只读本地 ETF 份额、估算净申赎、沪深300行情和量价背离分析，重点为其他 AI 提供择时投机与获利了结决策依据。",
        instructions=(
            "其他 AI 快速决策指南：\n"
            "1. 一站式全景快照：调用 get_fund_pulse_snapshot 获取最新资金脉搏、沪深300对比与核心决策摘要；\n"
            "2. 择时投机/做多：调用 get_speculation_signals 获取 0-100 分量化评分、做多建议 (STRONG_SPECULATIVE_BUY等)、具体建仓仓位及优选 ETF；\n"
            "3. 获利了结/防守：调用 get_profit_taking_signals 获取出局紧迫度评分、建议减仓比例及移动止盈参考点位；\n"
            "4. 宏观协同：调用 get_csi300_correlation_analysis 查看资金流与沪深300的相关性及历史背离记录；\n"
            "5. 基础与深度查询：按需调用 get_market_timing_brief、get_radar_overview、get_daily_series、get_etf_detail 等。"
        ),
        version="1.1.0",
    )
    cache_file = DATA / "market_data.json"
    evidence_file = ROOT / "evidence.csv"
    cache_lock = threading.RLock()
    last_market_signature: tuple[int, int] | None = None
    last_evidence_signature: tuple[int, int] | None = None
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
                try:
                    evidence_stat = evidence_file.stat()
                    evidence_signature = (evidence_stat.st_mtime_ns, evidence_stat.st_size)
                except FileNotFoundError:
                    evidence_signature = None
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

    @mcp.tool()
    def list_etf_universe() -> dict[str, Any]:
        """List the fixed monitored broad-index ETF universe; available before the first market-data refresh."""
        return {"schema_version": 1, "etfs": ETF_UNIVERSE,
                "identity_policy": "名单表示监测范围，不能由 ETF 名单或份额单独确认交易者身份"}

    @mcp.tool()
    def get_radar_overview(end_date: str | None = None, window: int = 20,
                           z_threshold: float = 2.0, min_flow_yi: float = 10.0,
                           sync_min: int = 3) -> dict[str, Any]:
        """Read the prioritized radar state: latest disclosure, last complete ETF flow, CSI300, alerts, coverage and freshness. End date is an optional historical cutoff (YYYY-MM-DD)."""
        return _overview(read_snapshot(end_date=end_date, window=window,
                                   z_threshold=z_threshold, min_flow_yi=min_flow_yi, sync_min=sync_min))

    @mcp.tool()
    def get_market_timing_brief(end_date: str | None = None, window: int = 20,
                                z_threshold: float = 2.0, min_flow_yi: float = 10.0,
                                sync_min: int = 3) -> dict[str, Any]:
        """Evaluate entry and profit-taking/risk-reduction WATCH conditions separately. Returns exact threshold checks, CSI300 context and blockers; historical or incomplete data never masquerades as a current signal. No return guarantee."""
        return _timing_brief(read_snapshot(end_date=end_date, window=window,
                                       z_threshold=z_threshold, min_flow_yi=min_flow_yi, sync_min=sync_min))

    @mcp.tool()
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

    @mcp.tool()
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

    @mcp.tool()
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

    @mcp.tool()
    def get_data_quality(end_date: str | None = None) -> dict[str, Any]:
        """Inspect coverage, missing/quarantined ETF rows, source modes/timestamps, stale refresh state, and source documentation. No credentials are returned."""
        return _quality(read_snapshot(end_date=end_date))

    @mcp.tool()
    def get_methodology(end_date: str | None = None, window: int = 20,
                        z_threshold: float = 2.0, min_flow_yi: float = 10.0,
                        sync_min: int = 3) -> dict[str, Any]:
        """Read exact share-to-flow formula, units, rolling baseline, split quarantine, synchrony criteria, limitations, and current thresholds."""
        snap = read_snapshot(end_date=end_date, window=window,
                         z_threshold=z_threshold, min_flow_yi=min_flow_yi, sync_min=sync_min)
        return {"schema_version": 1, "context": _context(snap), "config": snap.get("config"),
                "method": snap.get("method"), "sources": snap.get("sources", [])}

    @mcp.tool()
    def get_speculation_signals(
        end_date: str | None = None,
        risk_preference: str = "balanced",
        window: int = 20,
        z_threshold: float = 2.0,
        min_flow_yi: float = 10.0,
        sync_min: int = 3,
    ) -> dict[str, Any]:
        """【择时投机重点信号】为其他 AI 产出明确的买入/投机评分 (0-100分)、推荐操作 (STRONG_SPECULATIVE_BUY/SPECULATIVE_BUY/ACCUMULATE/AVOID)、具体建仓仓位建议、量价底背离托底检测、止损与止盈点位参考。"""
        if risk_preference not in {"aggressive", "balanced", "conservative"}:
            risk_preference = "balanced"
        snap = read_snapshot(end_date=end_date, window=window, z_threshold=z_threshold, min_flow_yi=min_flow_yi, sync_min=sync_min)
        return _speculation_signals(snap, risk_preference=risk_preference)

    @mcp.tool()
    def get_profit_taking_signals(
        end_date: str | None = None,
        holding_return_pct: float | None = None,
        cost_benchmark_level: float | None = None,
        window: int = 20,
        z_threshold: float = 2.0,
        min_flow_yi: float = 10.0,
        sync_min: int = 3,
    ) -> dict[str, Any]:
        """【获利了结重点信号】评估宽基资金赎回压力、高位顶背离与撤退风险，给出获利了结紧迫度评分 (0-100分)、明确出局操作 (STRONG_TAKE_PROFIT/PARTIAL_TAKE_PROFIT/TRAILING_STOP/HOLD_SAFE)、建议减仓比例及移动止盈规则。"""
        snap = read_snapshot(end_date=end_date, window=window, z_threshold=z_threshold, min_flow_yi=min_flow_yi, sync_min=sync_min)
        return _profit_taking_signals(snap, holding_return_pct=holding_return_pct, cost_benchmark_level=cost_benchmark_level)

    @mcp.tool()
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

    @mcp.tool()
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
        """Latest speculation and timing decision with position sizing."""
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
        """Prompt to formulate an actionable market speculation strategy based on current fund pulse and CSI 300."""
        return (
            "请作为专业量化投资总监，调用 get_speculation_signals 和 get_fund_pulse_snapshot 工具，"
            "分析最新的宽基 ETF 资金脉搏与沪深300指数价格背景。"
            "输出一份针对当前市场的【择时投机策略报告】：\n"
            "1. 资金脉搏当前定性与评分 (0-100)\n"
            "2. 是否存在量价背离（如指数下跌但大额资金托底抄底）\n"
            "3. 明确的交易行动指令（强烈做多 / 逢低试探 / 温和定投 / 规避观望）\n"
            "4. 具体的建仓仓位建议与优选 ETF 标的列表\n"
            "5. 沪深300指数的关键支撑位、目标止盈位与失效止损线。"
        )

    @mcp.prompt()
    def profit_taking_strategy(user_return_pct: float = 10.0) -> str:
        """Prompt to formulate an exit and profit-taking strategy for existing holdings."""
        return (
            f"请作为专业量化风控专家，假设当前投资者的宽基持仓浮盈约为 {user_return_pct}%。"
            "调用 get_profit_taking_signals 工具，检查最新的宽基赎回压力与沪深300高位顶背离风险。"
            "输出一份【获利了结与仓位防守指引】：\n"
            "1. 当前赎回压力与出局紧迫度评分 (0-100)\n"
            "2. 是否存在高位顶背离或跨基金集中撤退抛压\n"
            "3. 明确的了结建议（坚决止盈 / 分批落袋 / 移动止盈 / 安心持有）\n"
            "4. 建议减仓比例（如减仓 50%）与移动止盈保护点位。"
        )

    return mcp


def main() -> None:
    """Run the local-only MCP stdio process for an MCP host."""
    create_mcp_server().run(transport="stdio")


if __name__ == "__main__":
    main()
