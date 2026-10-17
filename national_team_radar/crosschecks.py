"""Optional, independently labelled Tushare corroboration channels.

These observations never raise the ETF signal score or identify a buyer behind
ETF creations. Missing permissions are represented as unavailable, never zero.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
import json
import os
from pathlib import Path
import re

import pandas as pd

from .provider import _client, _request, _safe_error, DataProviderError
from .disclosures import CN

DATA = Path(__file__).resolve().parent / "data"
DEFAULT_STOCKS = ["601398.SH", "601939.SH", "601288.SH", "601988.SH", "601318.SH", "600030.SH"]
INSTITUTION = re.compile(r"中央汇金(?:投资|资产管理)|中国证券金融|全国社保基金|全国社会保障基金|基本养老保险基金")
DEFINITIONS = {
    "holders": {"name": "上市公司股东披露", "endpoint": "top10_holders", "docs": "https://tushare.pro/document/2?doc_id=61", "limit": "仅样本股前十大股东；非全A股、非ETF持有人；报告期与公告日分别记录，未出现不视为清仓"},
    "dragon_tiger": {"name": "龙虎榜机构席位", "endpoint": "top_inst", "docs": "https://tushare.pro/document/2?doc_id=107", "limit": "仅最近5个交易日上榜机构席位；同一交易可能重复上榜，不合计为市场净流入；机构专用不等于国家队"},
    "block_trades": {"name": "大宗交易", "endpoint": "block_trade", "docs": "https://tushare.pro/document/2?doc_id=161", "limit": "最近5个交易日全市场公开大宗成交；每笔都有买卖双方，不能合计成净流入或识别最终受益人"},
    "large_orders": {"name": "样本蓝筹超大单", "endpoint": "moneyflow", "docs": "https://tushare.pro/document/2?doc_id=170", "limit": "仅样本股票最近5个交易日，主动大单口径与ETF申赎口径不同；不代表机构身份"},
}


def _iso(value) -> str | None:
    parsed = pd.to_datetime(str(value), errors="coerce")
    return parsed.date().isoformat() if not pd.isna(parsed) else None


def _number(value):
    value = pd.to_numeric(value, errors="coerce")
    return float(value) if pd.notna(value) and abs(float(value)) != float("inf") else None


def _rows(frame: pd.DataFrame, kind: str, cutoff: str) -> list[dict]:
    result = []
    for raw in frame.to_dict(orient="records"):
        day = _iso(raw.get("ann_date") if kind == "holders" else raw.get("trade_date"))
        if not day or day > cutoff:
            continue
        item = {"date": day, "ts_code": raw.get("ts_code"), "affects_score": False}
        if kind == "holders":
            if not INSTITUTION.search(str(raw.get("holder_name", ""))):
                continue
            item.update(holder_name=str(raw["holder_name"]), publication_date=day,
                        report_period=_iso(raw.get("end_date")), hold_amount_shares=_number(raw.get("hold_amount")),
                        hold_ratio_pct=_number(raw.get("hold_ratio")))
        elif kind == "dragon_tiger":
            if "机构专用" not in str(raw.get("exalter", "")):
                continue
            item.update(seat=str(raw.get("exalter", "")), reason=str(raw.get("reason", "")), side=str(raw.get("side", "")),
                        buy_yuan=_number(raw.get("buy")), sell_yuan=_number(raw.get("sell")), net_buy_yuan=_number(raw.get("net_buy")))
        elif kind == "block_trades":
            price, vol = _number(raw.get("price")), _number(raw.get("vol"))
            item.update(price_yuan=price, volume_wan_shares=vol,
                        amount_yi=price * vol / 10000 if price is not None and vol is not None else None,
                        buyer_seat=str(raw.get("buyer", "")), seller_seat=str(raw.get("seller", "")))
        else:
            buy, sell = _number(raw.get("buy_elg_amount")), _number(raw.get("sell_elg_amount"))
            item.update(buy_elg_wan=buy, sell_elg_wan=sell,
                        elg_active_difference_yi=(buy - sell) / 10000 if buy is not None and sell is not None else None,
                        vendor_net_mf_wan=_number(raw.get("net_mf_amount")))
        result.append(item)
    return result


def load_crosschecks() -> dict:
    try:
        return json.loads((DATA / "crosschecks.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"channels": {key: {**definition, "state": "not_fetched", "rows": []} for key, definition in DEFINITIONS.items()}}


def refresh_crosschecks(calendar: list[str], end: str, force: bool = False) -> dict:
    from .server import atomic_json
    cached = load_crosschecks()
    now = datetime.now(CN)
    stocks = [s.strip().upper() for s in os.environ.get("RADAR_STOCK_UNIVERSE", ",".join(DEFAULT_STOCKS)).split(",")]
    stocks = list(dict.fromkeys(s for s in stocks if re.fullmatch(r"\d{6}\.(SH|SZ)", s)))[:30]
    try:
        if not force and cached.get("requested_end") == end and now - datetime.fromisoformat(cached["checked_at"]) < timedelta(hours=2):
            return cached
    except (KeyError, ValueError, TypeError):
        pass
    sessions = [d for d in calendar if d <= end][-5:]
    stamp = now.isoformat(timespec="seconds")
    try:
        client, token, _ = _client()
        client_error = None
    except DataProviderError as exc:
        client, token, client_error = None, "", str(exc)

    def collect(key: str) -> tuple[str, dict]:
        definition = DEFINITIONS[key]
        records, errors, completed = [], [], []
        queries = ([{"ts_code": s, "start_date": (date.fromisoformat(end) - timedelta(days=550)).strftime("%Y%m%d"), "end_date": end.replace("-", "")} for s in stocks]
                   if key == "holders" else
                   [{"ts_code": s, "start_date": sessions[0].replace("-", ""), "end_date": sessions[-1].replace("-", "")} for s in stocks]
                   if key == "large_orders" and sessions else
                   [{"trade_date": d.replace("-", "")} for d in sessions])
        if client is None:
            errors.append(client_error)
        else:
            for query in queries:
                try:
                    frame = _request(client, definition["endpoint"], token, **query)
                    cap = 1000 if key == "block_trades" else 10000 if key == "dragon_tiger" else 6000 if key == "large_orders" else 2000
                    if len(frame) >= cap:
                        raise DataProviderError("返回达到接口上限，覆盖不完整，拒绝标为完整成功")
                    records.extend(_rows(frame, key, end))
                    completed.append(query.get("ts_code") or query.get("trade_date"))
                except Exception as exc:
                    errors.append(_safe_error(exc, token))
                    # A permission failure is shared by the entire endpoint.
                    if any(word in errors[-1].lower() for word in ("权限", "不存在", "积分", "permission", "not exist")):
                        break
        unique = {json.dumps(row, sort_keys=True, ensure_ascii=False): row for row in records}
        records = sorted(unique.values(), key=lambda r: (r["date"], r.get("amount_yi") or 0), reverse=True)
        old = (cached.get("channels") or {}).get(key, {})
        stale = bool(errors and not records and old.get("rows"))
        return key, {**definition, "state": "stale_cache" if stale else "partial" if errors and completed else "unavailable" if errors else "ok" if queries else "not_fetched",
                     "checked_at": stamp, "data_fetched_at": old.get("data_fetched_at") if stale else stamp,
                     "sample_stocks": stocks if key in {"holders", "large_orders"} else None,
                     "sessions": sessions, "completed_queries": completed, "errors": errors[:5],
                     "total_records": len(records) if not stale else old.get("total_records", len(old.get("rows", []))),
                     "rows": old.get("rows", []) if stale else records[:500],
                     "truncated": len(records) > 500, "identity_confirmed": False}
    with ThreadPoolExecutor(max_workers=4) as pool:
        channels = dict(pool.map(collect, DEFINITIONS))
    result = {"checked_at": stamp, "requested_end": end, "channels": channels,
              "policy": "辅助渠道不自动加分；公告日期早于截止日才可用；当前源修订未重建历史可见性"}
    atomic_json(DATA / "crosschecks.json", result)
    return result


def view_crosschecks(end: str) -> dict:
    result = load_crosschecks()
    for channel in result.get("channels", {}).values():
        channel["rows"] = [row for row in channel.get("rows", []) if row.get("date", "") <= end]
        channel["returned"] = len(channel["rows"])
    return result
