"""Conservative trade-date analytics for broad-market ETF share flows.

Input ``total_share`` is in 万份 and NAV is yuan per share.  Nothing in this
module attributes subscriptions/redemptions to a particular market participant.
The engine deliberately keeps missing observations missing.
Historical inputs may contain revisions; this is not a point-in-time backtest.
"""

from __future__ import annotations

import math
from collections import Counter, deque
from datetime import date, datetime
from statistics import mean, stdev
from typing import Any, Iterable, Mapping
from urllib.parse import urlparse
from .universe import LISTING_DATES


DEFAULT_UNIVERSE = [
    {"ts_code": "510300.SH", "name": "华泰柏瑞沪深300ETF", "index": "沪深300"},
    {"ts_code": "510310.SH", "name": "易方达沪深300ETF", "index": "沪深300"},
    {"ts_code": "510330.SH", "name": "华夏沪深300ETF", "index": "沪深300"},
    {"ts_code": "159919.SZ", "name": "嘉实沪深300ETF", "index": "沪深300"},
    {"ts_code": "510050.SH", "name": "华夏上证50ETF", "index": "上证50"},
    {"ts_code": "510500.SH", "name": "南方中证500ETF", "index": "中证500"},
    {"ts_code": "512100.SH", "name": "南方中证1000ETF", "index": "中证1000"},
]

DEFAULT_CONFIG = {
    "window": 20,
    "z_threshold": 2.0,
    "min_flow_yi": 10.0,
    "sync_min": 3,
    "share_jump_pct": 20.0,
}

_QUALITY_TEXT = {
    "pre_listing_session": "基金尚未上市，份额与净值仅作计算锚点，不参与资金信号或Z基准",
    "missing_current_observation": "缺少当日观测",
    "missing_current_share": "缺少当日有效份额",
    "missing_previous_session": "缺少上一交易日，不能跨日作差",
    "missing_previous_share": "缺少上一交易日有效份额",
    "missing_previous_nav": "缺少上一交易日有效净值",
    "conflicting_duplicate": "同日重复记录冲突，已隔离",
    "large_share_jump": "份额大幅变化，需核验真实大额申赎及份额调整",
    "split_like_share_nav_jump": "份额与净值大幅反向变化且规模近似不变，疑似拆分或合并，已隔离",
    "share_jump_pending_nav": "份额大幅变化，缺少当日净值核验，暂不计入资金估算",
    "insufficient_baseline": "前一完整窗口的交易日样本不足",
    "zero_baseline_variance": "历史样本标准差为零，无法计算Z分数",
    "incomplete_universe": "监控组合数据不完整，合计资金不计算",
}


def _date(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (date, datetime)):
        return value.strftime("%Y-%m-%d")
    value = str(value).strip()
    try:
        if len(value) == 8 and value.isdigit():
            return datetime.strptime(value, "%Y%m%d").strftime("%Y-%m-%d")
        return date.fromisoformat(value[:10]).isoformat()
    except (ValueError, TypeError):
        return None


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _positive(value: Any) -> float | None:
    value = _number(value)
    return value if value is not None and value > 0 else None


def _records(observations: Any) -> list[dict[str, Any]]:
    if observations is None:
        return []
    if hasattr(observations, "to_dict"):
        return observations.to_dict(orient="records")
    return [dict(item) for item in observations]


def _universe(universe: Any) -> list[dict[str, str]]:
    if universe is None:
        universe = DEFAULT_UNIVERSE
    if isinstance(universe, Mapping):
        universe = [
            {"ts_code": key, **(dict(value) if isinstance(value, Mapping) else {"name": str(value)})}
            for key, value in universe.items()
        ]
    result = []
    seen = set()
    defaults = {item["ts_code"]: item for item in DEFAULT_UNIVERSE}
    for item in universe:
        item = {"ts_code": item} if isinstance(item, str) else dict(item)
        code = str(item.get("ts_code", item.get("code", ""))).strip().upper()
        if not code or code in seen:
            raise ValueError("universe 必须包含非空且不重复的 ts_code")
        seen.add(code)
        known = defaults.get(code, {})
        result.append({
            "ts_code": code,
            "name": str(item.get("name") or known.get("name") or code),
            "index": str(item.get("index") or item.get("benchmark") or known.get("index") or ""),
            "list_date": _date(item.get("list_date") or known.get("list_date") or LISTING_DATES.get(code)),
        })
    if not result:
        raise ValueError("universe 不能为空")
    return result


def _configuration(config: Mapping[str, Any] | None) -> dict[str, Any]:
    result = {**DEFAULT_CONFIG, **{k: v for k, v in (config or {}).items() if k in DEFAULT_CONFIG}}
    for key in ("window", "sync_min"):
        numeric = _number(result[key])
        if numeric is None or numeric != int(numeric) or numeric < (2 if key == "window" else 1):
            raise ValueError(f"{key} 必须为有效正整数（window 至少为 2）")
        result[key] = int(numeric)
    for key in ("z_threshold", "min_flow_yi", "share_jump_pct"):
        numeric = _positive(result[key])
        if numeric is None:
            raise ValueError(f"{key} 必须为有限正数")
        result[key] = numeric
    return result


def _zscore(value: float | None, history: deque, window: int) -> tuple[float | None, str | None]:
    if value is None:
        return None, None
    if len(history) < window or any(item is None for item in history):
        return None, "insufficient_baseline"
    deviation = stdev(history)
    if deviation == 0:
        return None, "zero_baseline_variance"
    return (value - mean(history)) / deviation, None


def _status(flow: float | None, zscore: float | None, config: dict, sync: int | None = None) -> str:
    if flow is None or zscore is None:
        return "数据不足"
    qualified = abs(flow) >= config["min_flow_yi"] and abs(zscore) >= config["z_threshold"]
    # Direction must agree: a positive flow below a large positive baseline is
    # not a redemption alert, and vice versa.
    qualified = qualified and flow * zscore > 0
    if sync is not None:
        qualified = qualified and sync >= config["sync_min"]
    if qualified:
        return "大额净申购异动" if flow > 0 else "大额净赎回异动"
    return "观察"


def _quality(codes: Iterable[str]) -> list[str]:
    return [_QUALITY_TEXT.get(code, code) for code in codes]


def _evidence(items: Any, as_of: str | None) -> tuple[list[dict], list[str]]:
    result, warnings = [], []
    for item in items or []:
        if not isinstance(item, Mapping):
            warnings.append("已忽略格式无效的佐证线索")
            continue
        event_date = _date(item.get("date"))
        title = str(item.get("title") or "").strip()
        kind = str(item.get("kind") or "market").lower()
        url = str(item.get("url") or "").strip()
        parsed = urlparse(url)
        if (not event_date or not title or kind not in {"official", "holding", "market"}
                or (url and (parsed.scheme not in {"http", "https"} or not parsed.netloc))):
            warnings.append("已忽略日期、类型、标题或链接格式无效的佐证线索")
            continue
        # A historical snapshot must never borrow evidence from a later day.
        if as_of is None or event_date > as_of:
            warnings.append("已排除晚于数据截止日的佐证线索")
            continue
        official = item.get("verification") == "official_source" and item.get("source_id") in {"huijin", "csf", "ssf"}
        result.append({
            "date": event_date,
            "title": title,
            "url": url,
            "kind": kind,
            "source": item.get("source_id") if official else "manual",
            "source_name": item.get("source_name") if official else "人工记录",
            "verification": "official_source" if official else "人工录入，未自动核验原文",
            **({key: item.get(key) for key in ("publication_date", "first_seen_at", "fetched_at", "entity", "excerpt", "action", "attribution_limit")} if official else {}),
            "historical_visibility": "仅按公告日期过滤；不代表当时已被本系统抓取",
            "affects_score": False,
        })
    return sorted(result, key=lambda item: (item["date"], item["title"]), reverse=True), warnings


def analyze(observations: Any, calendar: Iterable[Any], universe: Any = None,
            config: Mapping[str, Any] | None = None, evidence: Any = None) -> dict[str, Any]:
    """Return a JSON-safe snapshot with a strictly trailing session baseline.

    ``calendar`` must contain true exchange sessions, including each observation's
    immediately preceding exchange session. Missing sessions are never inferred
    from weekday rules. Z requires valid flows for every one of the *previous*
    ``window`` exchange sessions and uses sample standard deviation (ddof=1).
    Missing sessions occupy their slots, rather than borrowing older values.
    Input revisions and publication timing are not reconstructed here.
    All raw and computed values are finite or ``None``.
    """
    config = _configuration(config)
    universe = _universe(universe)
    codes = [item["ts_code"] for item in universe]
    code_set = set(codes)
    sessions = sorted({value for item in calendar if (value := _date(item)) is not None})
    session_set = set(sessions)
    warnings = []
    observations_by_key = {}
    duplicates = set()
    off_calendar, malformed, irrelevant = 0, 0, 0
    for raw in _records(observations):
        day = _date(raw.get("trade_date", raw.get("date")))
        code = str(raw.get("ts_code", "")).strip().upper()
        if code not in code_set:
            irrelevant += 1
            continue
        if day is None:
            malformed += 1
            continue
        if day not in session_set:
            off_calendar += 1
            continue
        item = {key: _number(raw.get(key)) for key in ("total_share", "nav", "close", "amount", "pct_chg")}
        item["source"] = str(raw.get("source") or "")
        item["nav_ann_date"] = _date(raw.get("nav_ann_date"))
        key = (code, day)
        if key in observations_by_key and observations_by_key[key] != item:
            duplicates.add(key)
        observations_by_key[key] = item
    if off_calendar:
        warnings.append(f"已排除 {off_calendar} 条非交易日日历内的观测")
    if malformed:
        warnings.append(f"已排除 {malformed} 条日期无效的观测")
    if duplicates:
        warnings.append(f"发现 {len(duplicates)} 个同日数据冲突，相关比较已隔离")
    observed_days = sorted({day for _, day in observations_by_key})
    share_days = sorted({day for (_, day), item in observations_by_key.items() if _positive(item.get("total_share")) is not None})
    quote_days = sorted({day for (_, day), item in observations_by_key.items() if _positive(item.get("close")) is not None})
    as_of = share_days[-1] if share_days else None
    latest_quote_date = quote_days[-1] if quote_days else None
    calendar_latest_session = sessions[-1] if sessions else None
    pending_dates = [day for day in sessions if as_of is None or day > as_of]
    quote_pending_dates = [day for day in sessions if latest_quote_date is None or day > latest_quote_date]
    first_day = observed_days[0] if observed_days else None
    days = [day for day in sessions if first_day is not None and as_of is not None and first_day <= day <= as_of]
    previous_session = {day: sessions[i - 1] if i else None for i, day in enumerate(sessions)}
    etf_series = {}
    etfs = []
    for definition in universe:
        code = definition["ts_code"]
        series = []
        history = deque(maxlen=config["window"])
        for day in days:
            current = observations_by_key.get((code, day))
            previous_day = previous_session[day]
            previous = observations_by_key.get((code, previous_day)) if previous_day else None
            quality_codes = []
            if definition.get("list_date") and day < definition["list_date"]:
                quality_codes.append("pre_listing_session")
            current_share = _positive(current.get("total_share")) if current else None
            previous_share = _positive(previous.get("total_share")) if previous else None
            previous_nav = _positive(previous.get("nav")) if previous else None
            if current is None:
                quality_codes.append("missing_current_observation")
            elif current_share is None:
                quality_codes.append("missing_current_share")
            if previous is None:
                quality_codes.append("missing_previous_session")
            elif previous_share is None:
                quality_codes.append("missing_previous_share")
            if previous is not None and previous_nav is None:
                quality_codes.append("missing_previous_nav")
            if (code, day) in duplicates or (code, previous_day) in duplicates:
                quality_codes.append("conflicting_duplicate")
            delta = current_share - previous_share if current_share is not None and previous_share is not None else None
            share_change = delta / previous_share * 100 if delta is not None else None
            raw_flow = delta * previous_nav / 10_000 if delta is not None and previous_nav is not None else None
            current_nav = _positive(current.get("nav")) if current else None
            large_jump = share_change is not None and abs(share_change) > config["share_jump_pct"]
            split_like = False
            if large_jump and current_nav is not None and previous_nav is not None:
                share_ratio = current_share / previous_share
                nav_ratio = current_nav / previous_nav
                # A large legitimate creation leaves NAV per share broadly
                # unchanged. A split/merge changes shares and NAV reciprocally.
                split_like = ((share_ratio - 1) * (nav_ratio - 1) < 0
                              and abs(share_ratio * nav_ratio - 1) <= 0.03
                              and abs(nav_ratio - 1) >= 0.15)
                if split_like:
                    quality_codes.append("split_like_share_nav_jump")
            if large_jump and current_nav is None:
                quality_codes.append("share_jump_pending_nav")
            flow = raw_flow if not quality_codes else None
            if large_jump:
                quality_codes.append("large_share_jump")
            baseline_days = sum(value is not None for value in history)
            z_score, z_reason = _zscore(flow, history, config["window"])
            if z_reason:
                quality_codes.append(z_reason)
            row = {
                "date": day,
                "flow_yi": flow,
                "raw_flow_yi": raw_flow,
                "z_score": z_score,
                "baseline_days": baseline_days,
                "baseline_reason": _QUALITY_TEXT.get(z_reason) if z_reason else None,
                "total_share_yi": current_share / 10_000 if current_share is not None else None,
                "delta_share_yi": delta / 10_000 if delta is not None else None,
                "share_change_pct": share_change,
                "nav": current_nav,
                "previous_nav": previous_nav,
                "nav_ann_date": current.get("nav_ann_date") if current else None,
                "previous_nav_ann_date": previous.get("nav_ann_date") if previous else None,
                "close": current.get("close") if current else None,
                "amount": current.get("amount") if current else None,
                "pct_chg": current.get("pct_chg") if current else None,
                "source": current.get("source") if current else None,
                "valid": flow is not None,
                "quarantined": split_like or "conflicting_duplicate" in quality_codes or "share_jump_pending_nav" in quality_codes,
                "quality": _quality(quality_codes),
                "quality_codes": quality_codes,
                "status": _status(flow, z_score, config),
            }
            series.append(row)
            history.append(flow)
        etf_series[code] = series
        latest = series[-1] if series else {
            "date": None, "flow_yi": None, "z_score": None, "total_share_yi": None,
            "delta_share_yi": None, "share_change_pct": None, "close": None,
            "pct_chg": None, "status": "数据不足", "valid": False,
            "quarantined": False, "quality": ["尚无交易日数据"],
            "quality_codes": ["no_observations"], "baseline_days": 0,
        }
        etfs.append({**definition, **latest})

    portfolio_series = []
    portfolio_history = deque(maxlen=config["window"])
    alerts = []
    observation_events = []
    for i, day in enumerate(days):
        members = [etf_series[code][i] for code in codes]
        valid_count = sum(member["valid"] for member in members)
        complete = valid_count == len(codes)
        flow = sum(member["flow_yi"] for member in members) if complete else None
        positive = sum(max(member["flow_yi"], 0) for member in members) if complete else None
        negative = sum(min(member["flow_yi"], 0) for member in members) if complete else None
        sync_in = sum(member["status"] == "大额净申购异动" for member in members)
        sync_out = sum(member["status"] == "大额净赎回异动" for member in members)
        baseline_days = sum(value is not None for value in portfolio_history)
        z_score, reason = _zscore(flow, portfolio_history, config["window"])
        quality_codes = [] if complete else ["incomplete_universe"]
        if reason:
            quality_codes.append(reason)
        sync_count = sync_in if flow is not None and flow > 0 else sync_out
        status = _status(flow, z_score, config, sync_count)
        row = {
            "date": day, "flow_yi": flow, "z_score": z_score,
            "positive_flow_yi": positive, "negative_flow_yi": negative,
            "valid_count": valid_count, "total_count": len(codes),
            "sync_in": sync_in, "sync_out": sync_out,
            "positive_count": sum(member["flow_yi"] is not None and member["flow_yi"] > 0 for member in members),
            "negative_count": sum(member["flow_yi"] is not None and member["flow_yi"] < 0 for member in members),
            "status": status, "complete": complete, "baseline_days": baseline_days,
            "baseline_reason": _QUALITY_TEXT.get(reason) if reason else None,
            "quality": _quality(quality_codes), "quality_codes": quality_codes,
            "sync_observation": None,
        }
        portfolio_series.append(row)
        for cumulative_window in (5, 20):
            recent = portfolio_series[-cumulative_window:]
            row[f"flow_{cumulative_window}d_yi"] = (
                sum(item["flow_yi"] for item in recent)
                if len(recent) == cumulative_window and all(item["complete"] for item in recent) else None)
        if status in {"大额净申购异动", "大额净赎回异动"}:
            alerts.append({
                "date": day, "direction": "in" if flow > 0 else "out",
                "flow_yi": flow, "z_score": z_score, "sync_count": sync_count,
                "title": status,
                "description": f"{sync_count} 只ETF同向异常；买卖方身份未确认",
            })
        for direction, count in (("in", sync_in), ("out", sync_out)):
            if count >= config["sync_min"] and status != ("大额净申购异动" if direction == "in" else "大额净赎回异动"):
                title = "多只ETF同步净申购观察" if direction == "in" else "多只ETF同步净赎回观察"
                description = f"{count} 只ETF同向异常，但组合整体未满足同向异动条件；组合净额可能被其他ETF抵消，交易者身份未确认"
                observation_events.append({
                    "date": day, "kind": "synchrony_observation", "direction": direction,
                    "flow_yi": flow, "z_score": z_score, "sync_count": count,
                    "title": title, "description": description,
                })
                row["sync_observation"] = (row["sync_observation"] + "；" if row["sync_observation"] else "") + title
        portfolio_history.append(flow)

    summary = dict(portfolio_series[-1]) if portfolio_series else {
        "date": None, "flow_yi": None, "z_score": None, "valid_count": 0,
        "total_count": len(codes), "sync_in": 0, "sync_out": 0,
        "status": "数据不足", "complete": False, "baseline_days": 0,
        "baseline_reason": "尚无交易日数据", "quality": ["尚无交易日数据"],
    }
    for window in (5, 20):
        recent = portfolio_series[-window:]
        summary[f"flow_{window}d_yi"] = (
            sum(item["flow_yi"] for item in recent)
            if len(recent) == window and all(item["complete"] for item in recent) else None
        )
    summary["identity_confirmed"] = False
    summary["interpretation"] = "宽基ETF一级市场净申赎估算；不能识别交易者身份"
    evidence_items, evidence_warnings = _evidence(evidence, as_of)
    warnings.extend(evidence_warnings)
    missing_codes = [item["ts_code"] for item in etfs if not item["valid"]]
    quarantined_codes = [item["ts_code"] for item in etfs if item["quarantined"]]
    if missing_codes:
        warnings.append(f"最新交易日仅 {summary['valid_count']}/{len(codes)} 只ETF可比，组合合计留空")
    if quarantined_codes:
        warnings.append("最新交易日存在份额跳变或数据冲突，已隔离相关资金估算")
    if pending_dates:
        warnings.append(f"所选日历截至 {calendar_latest_session}，份额最新披露日为 {as_of or '暂无'}；后续交易日份额待披露或数据待更新")
    if quote_pending_dates:
        warnings.append(f"行情最新日期为 {latest_quote_date or '暂无'}，尚未覆盖所选日历的最新交易日 {calendar_latest_session}")
    quality_counts = Counter(code for values in etf_series.values() for item in values for code in item["quality_codes"])
    last_complete = next((dict(item) for item in reversed(portfolio_series) if item["complete"]), None)
    return {
        "schema_version": 1,
        "as_of": as_of,
        "summary": summary,
        "last_complete": last_complete,
        "series": portfolio_series,
        "etfs": etfs,
        "etf_series": etf_series,
        "alerts": list(reversed(alerts)),
        "observation_events": list(reversed(observation_events)),
        "quality": {
            "warnings": list(dict.fromkeys(warnings)),
            "coverage": {
                "valid_count": summary["valid_count"], "total_count": len(codes),
                "missing_codes": missing_codes, "quarantined_codes": quarantined_codes,
                "complete_days": sum(item["complete"] for item in portfolio_series),
                "total_days": len(days),
            },
            "issue_counts": dict(quality_counts),
            "excluded_non_session_rows": off_calendar,
            "excluded_invalid_date_rows": malformed,
            "ignored_outside_universe_rows": irrelevant,
            "conflicting_duplicates": len(duplicates),
            "latest_share_date": as_of,
            "latest_quote_date": latest_quote_date,
            "calendar_latest_session": calendar_latest_session,
            "pending_dates": pending_dates,
            "quote_pending_dates": quote_pending_dates,
        },
        "config": config,
        "evidence": evidence_items,
        "method": {
            "formula": "估算净申赎(亿元)=(当日总份额(万份)-上一交易日总份额(万份))×上一交易日单位净值(元)÷10000",
            "units": {"input_total_share": "万份", "total_share_yi": "亿份", "delta_share_yi": "亿份", "flow_yi": "亿元", "nav": "元/份", "amount": "千元", "share_change_pct": "%"},
            "baseline": f"使用当日前 {config['window']} 个交易日的资金估算值，窗口须完整；缺失不借用更早样本；不含当日；样本标准差(ddof=1)",
            "calendar": "只与真实交易日历中的上一交易日比较，缺失不跨日补差、不填零",
            "portfolio": "固定监控组合全部ETF可比时才求和；5日/20日合计要求连续对应交易日均完整",
            "synchrony": "同向ETF各自同时满足金额和Z阈值；组合还须满足自身金额、Z阈值及同步只数阈值；仅同步达标时单列观察事件，不认定组合异动",
            "share_jump": f"绝对份额变化超过 {config['share_jump_pct']:g}% 时复核当日净值；净值缺失先隔离。份额与净值反向变化、净值变幅≥15%且份额比×净值比距1≤3%时，按疑似拆分/合并隔离；不限制为整数比例，仍需公告核实",
            "evidence": "人工佐证线索按官方披露、持仓披露、市场辅助分类；未自动核验原文，不参与评分，排除晚于截止日的线索",
            "limitations": [
                "ETF总份额增减反映一级市场净申赎，估算额不等于现金净流入或国家队交易额",
                "二级市场买卖不直接改变总份额，但可能经套利申购间接影响；也可能完全不引起份额变化",
                "同一ETF资金指标派生出的金额、Z值与跨ETF同步性不构成独立身份佐证",
                "当日份额和净值披露可能滞后，应以数据截止日期和完整性为准",
                "份额与净值呈拆分式反向变化时会暂时隔离估算，仍需原始公告核验",
                "历史分析使用当前获取的数据版本；未重建全部披露时刻与历史修订，不是严格历史时点可见性回测",
            ],
        },
    }
