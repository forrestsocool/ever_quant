"""Tushare data access for the broad-market ETF flow radar.

Credentials are read locally and never written into this package. Shares are in
10,000 units, NAV/close in CNY and turnover in CNY 1,000. Missing disclosures stay
missing: a quote is never substituted for a NAV or a share observation.
"""

from __future__ import annotations

import ast
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
import time
from typing import Any
import warnings

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
CACHE_DIR = Path(__file__).resolve().parent / "data"
ETF_UNIVERSE = [
    {"ts_code": "510300.SH", "name": "华泰柏瑞沪深300", "index": "沪深300"},
    {"ts_code": "510310.SH", "name": "易方达沪深300", "index": "沪深300"},
    {"ts_code": "510330.SH", "name": "华夏沪深300", "index": "沪深300"},
    {"ts_code": "159919.SZ", "name": "嘉实沪深300", "index": "沪深300"},
    {"ts_code": "510050.SH", "name": "华夏上证50", "index": "上证50"},
    {"ts_code": "510500.SH", "name": "南方中证500", "index": "中证500"},
    {"ts_code": "512100.SH", "name": "南方中证1000", "index": "中证1000"},
]
OBSERVATION_COLUMNS = [
    "trade_date", "ts_code", "total_share", "nav", "close", "amount",
    "pct_chg", "source", "nav_ann_date",
]
DOCS = {
    "etf_share_size": "https://tushare.pro/document/2?doc_id=408",
    "fund_share": "https://tushare.pro/document/2?doc_id=207",
    "fund_nav": "https://tushare.pro/document/2?doc_id=119",
    "fund_daily": "https://tushare.pro/document/2?doc_id=127",
}
_LIMITS = {"etf_share_size": 5000, "fund_share": 2000, "fund_nav": 2000,
           "fund_daily": 5000, "index_daily": 5000, "trade_cal": 5000}
_SHANGHAI = timezone(timedelta(hours=8))
_CACHE_LOCK = threading.RLock()


class DataProviderError(RuntimeError):
    """A sanitized error safe for the UI or a local log."""


def _iso(value: str) -> str:
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            pass
    raise ValueError("日期必须为 YYYY-MM-DD 或 YYYYMMDD。")


def _now() -> str:
    return datetime.now(_SHANGHAI).isoformat(timespec="seconds")


def _credential() -> tuple[str, str]:
    """Do not import strategy scripts: inspect only literal set_token arguments."""
    for key in ("SXSC_TUSHARE_TOKEN", "TUSHARE_TOKEN"):
        value = os.environ.get(key, "").strip()
        if value:
            return value, "environment"
    credential_file = Path.home() / "jsdata_tk.csv"
    if credential_file.exists():
        try:
            frame = pd.read_csv(credential_file, dtype=str)
            value = str(frame.iloc[0]["token"]).strip()
            if value and value != "nan":
                return value, "local_saved_credential"
        except (OSError, ValueError, KeyError, IndexError):
            pass
    for filename in ("backtest.py", "dividend_strategy.py"):
        file = ROOT / filename
        if not file.exists():
            continue
        try:
            tree = ast.parse(file.read_text(encoding="utf-8-sig"))
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "set_token" and node.args
                        and isinstance(node.args[0], ast.Constant)
                        and isinstance(node.args[0].value, str) and node.args[0].value):
                    return node.args[0].value, "local_strategy_literal"
        except (OSError, SyntaxError, UnicodeError):
            continue
    raise DataProviderError(
        "未找到本地 Tushare 认证。可设置 SXSC_TUSHARE_TOKEN 或 TUSHARE_TOKEN，"
        "或沿用本项目原有认证；认证值不会写入雷达文件。"
    )


def _client() -> tuple[Any, str, str]:
    token, credential_source = _credential()
    local_library = ROOT / "tushare"
    if (local_library / "sxsc_tushare").is_dir():
        path = str(local_library)
        if path not in sys.path:
            sys.path.insert(0, path)
        module = importlib.import_module("sxsc_tushare")
        return module.get_api(token=token, env="prd", timeout=15), token, credential_source
    try:
        module = importlib.import_module("tushare")
        return module.pro_api(token=token, timeout=15), token, credential_source
    except (ImportError, AttributeError):
        raise DataProviderError("找不到可用的 Tushare 客户端，请安装 tushare 或恢复本地 sxsc_tushare。") from None


def _safe_error(exc: Exception, token: str = "") -> str:
    message = str(exc)
    if token:
        message = message.replace(token, "[认证信息已隐藏]")
    message = re.sub(r"(?i)(token[\s\"':=]+)[^\s,}\"']+", r"\1[已隐藏]", message)
    message = re.sub(r"\b[a-zA-Z0-9]{36,}\b", "[敏感标识已隐藏]", message)
    message = re.sub(r"https?://[^\s]+", "[服务地址]", message)
    return " ".join(message.split())[:180] or type(exc).__name__


def _permanent_error(message: str) -> bool:
    text = message.lower()
    return any(word in text for word in (
        "不存在", "无权限", "权限", "积分", "token", "认证", "invalid", "permission", "not exist",
    ))


def _request(client: Any, endpoint: str, token: str, **params: Any) -> pd.DataFrame:
    for attempt in range(2):
        try:
            result = getattr(client, endpoint)(**params)
            if not isinstance(result, pd.DataFrame):
                raise DataProviderError(f"{endpoint} 返回了无法识别的数据格式。")
            return result
        except Exception as exc:
            message = _safe_error(exc, token)
            if attempt == 1 or _permanent_error(message):
                raise DataProviderError(f"{endpoint}: {message}") from None
            time.sleep(0.35)
    raise AssertionError("unreachable")


def _fetch_range(client: Any, endpoint: str, start: str, end: str,
                 token: str, **params: Any) -> pd.DataFrame:
    """Date chunks + recursive cap detection; never assume a capped page is complete."""
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    frames: list[pd.DataFrame] = []
    while first <= last:
        chunk_end = min(first + timedelta(days=179), last)
        frame = _request(client, endpoint, token, start_date=first.strftime("%Y%m%d"),
                         end_date=chunk_end.strftime("%Y%m%d"), **params)
        if len(frame) >= _LIMITS.get(endpoint, 2000):
            if first == chunk_end:
                raise DataProviderError(f"{endpoint} 单日返回达到接口上限，无法确认数据完整性。")
            middle = first + (chunk_end - first) // 2
            left = _fetch_range(client, endpoint, first.isoformat(), middle.isoformat(), token, **params)
            right = _fetch_range(client, endpoint, (middle + timedelta(days=1)).isoformat(),
                                 chunk_end.isoformat(), token, **params)
            frames.extend([left, right])
        elif not frame.empty:
            frames.append(frame)
        first = chunk_end + timedelta(days=1)
    if not frames:
        return pd.DataFrame()
    # The vendor includes all-null optional columns in older NAV chunks. Keep
    # their schema without leaking an irrelevant pandas migration warning.
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="The behavior of DataFrame concatenation with empty or all-NA entries is deprecated", category=FutureWarning)
        return pd.concat(frames, ignore_index=True)


def _dated(frame: pd.DataFrame, date_column: str = "trade_date") -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame(columns=["trade_date", "ts_code"])
    if date_column not in frame:
        raise DataProviderError(f"接口返回缺少日期字段 {date_column}。")
    frame = frame.copy()
    frame["trade_date"] = pd.to_datetime(frame[date_column].astype(str), errors="coerce").dt.strftime("%Y-%m-%d")
    if frame["trade_date"].isna().any():
        raise DataProviderError("接口返回无法解析的日期，未采用这批记录。")
    if "ann_date" in frame:
        frame = frame.sort_values("ann_date")
    keys = ["trade_date"] + (["ts_code"] if "ts_code" in frame else [])
    # NAV may legitimately have later corrected disclosures. Other conflicts
    # must reach the caller as an error, rather than silently choosing a value.
    duplicate_keys = keys + (["ann_date"] if date_column == "nav_date" and "ann_date" in frame else [])
    for _, group in frame.loc[frame.duplicated(duplicate_keys, keep=False)].groupby(duplicate_keys, dropna=False):
        values = group.drop(columns=["update_flag"], errors="ignore").drop_duplicates()
        if len(values) > 1:
            raise DataProviderError("接口同日记录存在冲突，未采用这批记录。")
    return frame.drop_duplicates(keys, keep="last").sort_values("trade_date")


def _empty_observations() -> pd.DataFrame:
    return pd.DataFrame(columns=OBSERVATION_COLUMNS)


def _read_cache(key: str) -> tuple[pd.DataFrame, dict]:
    try:
        with _CACHE_LOCK:
            payload = (CACHE_DIR / f"{key}.csv").read_bytes()
            meta = json.loads((CACHE_DIR / f"{key}.json").read_text(encoding="utf-8"))
        if meta.get("version") not in {1, 2}:
            return pd.DataFrame(), {}
        if meta.get("sha256") and hashlib.sha256(payload).hexdigest() != meta["sha256"]:
            return pd.DataFrame(), {}
        from io import BytesIO
        frame = pd.read_csv(BytesIO(payload), dtype={"trade_date": str, "ts_code": str, "nav_ann_date": str})
        if "trade_date" not in frame or frame["trade_date"].isna().any():
            return pd.DataFrame(), {}
        if key not in {"calendar", "benchmark"}:
            if not set(OBSERVATION_COLUMNS).issubset(frame) or set(frame["ts_code"].dropna()) != {key}:
                return pd.DataFrame(), {}
            if frame.duplicated(["trade_date", "ts_code"]).any():
                return pd.DataFrame(), {}
            legacy_warnings = " ".join(meta.get("source_warnings", []))
            if "互联网" in legacy_warnings and "first_seen" not in " ".join(frame["source"].astype(str).unique()):
                # Older web fallbacks confused volume with turnover and NAV
                # date with publication date. Do not reuse that cache silently.
                return pd.DataFrame(), {}
        return frame, meta
    except (OSError, ValueError, TypeError, pd.errors.ParserError):
        return pd.DataFrame(), {}


def _write_cache(key: str, frame: pd.DataFrame, meta: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    csv_path = CACHE_DIR / f"{key}.csv"
    meta_path = CACHE_DIR / f"{key}.json"
    payload = frame.to_csv(index=False).encode("utf-8")
    manifest = {**meta, "version": 2, "sha256": hashlib.sha256(payload).hexdigest()}
    temporary_paths: list[Path] = []
    try:
        for suffix, content in ((".csv", payload), (".json", json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))):
            with tempfile.NamedTemporaryFile(dir=CACHE_DIR, prefix=f".{key}.", suffix=suffix, delete=False) as handle:
                temporary_paths.append(Path(handle.name))
                handle.write(content)
        # Unique temporary names avoid collisions with another refresh process;
        # the checksum rejects a torn pair after an interrupted replacement.
        with _CACHE_LOCK:
            temporary_paths[0].replace(csv_path)
            temporary_paths[1].replace(meta_path)
    finally:
        for temporary in temporary_paths:
            temporary.unlink(missing_ok=True)


def _segments(meta: dict) -> list[dict]:
    ranges = meta.get("ranges")
    if isinstance(ranges, list):
        return [r for r in ranges if isinstance(r, dict) and r.get("start") and r.get("end") and r.get("fetched_at")]
    if all(meta.get(key) for key in ("requested_start", "requested_end", "fetched_at")):
        return [{"start": meta["requested_start"], "end": meta["requested_end"], "fetched_at": meta["fetched_at"]}]
    return []


def _covers(ranges: list[dict], start: str, end: str) -> bool:
    try:
        next_day = date.fromisoformat(start)
        for item in sorted(ranges, key=lambda r: r["start"]):
            date.fromisoformat(item["start"])
            date.fromisoformat(item["end"])
            if item["end"] < next_day.isoformat():
                continue
            if item["start"] > next_day.isoformat():
                return False
            if item["end"] >= end:
                return True
            next_day = date.fromisoformat(item["end"]) + timedelta(days=1)
        return False
    except (ValueError, TypeError, KeyError):
        return False


def _manifest(old_meta: dict, start: str, end: str, fetched_at: str, **extra: Any) -> dict:
    # Keep independent retrieval times. Refreshing one overlapping week must not
    # make the entire old history appear freshly fetched.
    ranges = []
    for item in _segments(old_meta):
        if item["end"] < start or item["start"] > end:
            ranges.append(item)
        else:
            if item["start"] < start:
                ranges.append({**item, "end": (date.fromisoformat(start) - timedelta(days=1)).isoformat()})
            if item["end"] > end:
                ranges.append({**item, "start": (date.fromisoformat(end) + timedelta(days=1)).isoformat()})
    ranges.append({"start": start, "end": end, "fetched_at": fetched_at})
    return {"version": 2, "fetched_at": fetched_at, "requested_start": min(r["start"] for r in ranges),
            "requested_end": max(r["end"] for r in ranges), "ranges": ranges, **extra}


def _range_updated_at(meta: dict, start: str, end: str) -> str | None:
    stamps = [item["fetched_at"] for item in _segments(meta) if item["start"] <= end and item["end"] >= start]
    return min(stamps) if stamps else meta.get("fetched_at")


def _replace_range(old: pd.DataFrame, fresh: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    # Successful missing values supersede cached values inside the refreshed
    # interval. Never silently put an old NAV/share back into a fresh row.
    outside = old.loc[~old["trade_date"].between(start, end)] if not old.empty else pd.DataFrame()
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=FutureWarning)
        combined = pd.concat([outside, fresh], ignore_index=True)
    return combined.sort_values("trade_date").reset_index(drop=True)


def _cache_usable(meta: dict, start: str, end: str, force: bool) -> bool:
    if force or not meta:
        return False
    try:
        historical = date.fromisoformat(end) < datetime.now(_SHANGHAI).date() - timedelta(days=7)
        fresh_ranges = []
        for item in _segments(meta):
            age = datetime.now(_SHANGHAI) - datetime.fromisoformat(item["fetched_at"])
            if -300 <= age.total_seconds() < (86400 if historical else 3600):
                fresh_ranges.append(item)
        return _covers(fresh_ranges, start, end)
    except (ValueError, KeyError, TypeError):
        return False


def _slice(frame: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    if frame.empty or "trade_date" not in frame:
        return frame.iloc[:0].copy()
    return frame.loc[frame["trade_date"].between(start, end)].copy()


ENABLE_WEB_FALLBACK = True


def _web_fetch_benchmark(start: str, end: str) -> pd.DataFrame:
    """Fetch CSI 300 (000300.SH) index daily data from public web API as fallback."""
    try:
        import requests
        headers = {"Referer": "https://finance.sina.com.cn", "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        count = min(2000, max(300, (date.fromisoformat(end) - date.fromisoformat(start)).days + 20))
        url = f"https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol=sh000300&scale=240&ma=no&datalen={count}"
        r = requests.get(url, headers=headers, timeout=6)
        if r.status_code == 200 and r.json():
            df = pd.DataFrame(r.json())
            df["trade_date"] = df["day"]
            df["close"] = pd.to_numeric(df["close"], errors="coerce")
            df["pct_chg"] = df["close"].pct_change() * 100
            df["ts_code"] = "000300.SH"
            df["source"] = "sina_web_kline"
            return _dated(_slice(df, start, end))
    except Exception:
        pass
    return pd.DataFrame()


def _web_fetch_etf_daily(symbol: str, start: str, end: str) -> pd.DataFrame:
    """Fetch ETF daily quote from public web API as fallback."""
    try:
        import requests
        code, market = symbol.split(".")
        prefix = "sh" if market.upper() == "SH" else "sz"
        headers = {"Referer": "https://finance.sina.com.cn", "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        count = min(2000, max(300, (date.fromisoformat(end) - date.fromisoformat(start)).days + 20))
        url = f"https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol={prefix}{code}&scale=240&ma=no&datalen={count}"
        r = requests.get(url, headers=headers, timeout=6)
        if r.status_code == 200 and r.json():
            df = pd.DataFrame(r.json())
            df["trade_date"] = df["day"]
            df["close"] = pd.to_numeric(df["close"], errors="coerce")
            # KLine 'volume' is traded units, not turnover. Never manufacture
            # CNY turnover from a quantity or from quantity * closing price.
            df["amount"] = float("nan")
            df["pct_chg"] = df["close"].pct_change() * 100
            df["ts_code"] = symbol
            return _dated(_slice(df, start, end))
    except Exception:
        pass
    return pd.DataFrame()


def _web_fetch_etf_nav(symbol: str, start: str, end: str) -> pd.DataFrame:
    """Fetch ETF NAV history from Tiantian Jijin public web API as fallback."""
    try:
        import requests
        code = symbol.split(".")[0]
        headers = {"Referer": "http://fund.eastmoney.com/", "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        url = f"https://api.fund.eastmoney.com/f10/lsjz?fundCode={code}&pageIndex=1&pageSize=120"
        r = requests.get(url, headers=headers, timeout=6)
        if r.status_code == 200:
            rows = r.json().get("Data", {}).get("LSJZList", [])
            if rows:
                df = pd.DataFrame(rows)
                df["trade_date"] = df["FSRQ"]
                df["nav"] = pd.to_numeric(df["DWJZ"], errors="coerce")
                # This endpoint exposes valuation date but no publication
                # timestamp. Only claim that the NAV was visible when fetched.
                df["nav_ann_date"] = _now()[:10]
                df["ts_code"] = symbol
                return _dated(_slice(df, start, end), "trade_date")
    except Exception:
        pass
    return pd.DataFrame()


def _web_fetch_calendar(start: str, end: str) -> pd.DataFrame:
    """Fetch trading calendar from Sina/Akshare public API as fallback."""
    try:
        import akshare as ak
        cal = ak.tool_trade_date_hist_sina()
        cal["trade_date"] = pd.to_datetime(cal["trade_date"].astype(str)).dt.strftime("%Y-%m-%d")
        all_dates = pd.date_range(start, end).strftime("%Y-%m-%d").tolist()
        open_set = set(cal["trade_date"])
        if not open_set or (date.fromisoformat(end) - date.fromisoformat(max(open_set))).days > 3:
            return pd.DataFrame()  # Do not invent exchange closures beyond known history.
        open_list = sorted([d for d in open_set if d <= end])
        prev_map = {open_list[i]: open_list[i - 1] if i > 0 else open_list[0] for i in range(len(open_list))}
        rows = []
        for d in all_dates:
            is_open = 1 if d in open_set else 0
            rows.append({"trade_date": d, "is_open": is_open, "pretrade_date": prev_map.get(d, "") if is_open else ""})
        return pd.DataFrame(rows)
    except Exception:
        pass
    return pd.DataFrame()


def _symbol(client: Any, token: str, symbol: str, start: str, end: str,
            prefer_combined: bool) -> tuple[pd.DataFrame, list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    combined = pd.DataFrame()
    if prefer_combined:
        try:
            combined = _dated(_fetch_range(client, "etf_share_size", start, end, token,
                ts_code=symbol, fields="trade_date,ts_code,total_share,nav,close"))
        except DataProviderError as exc:
            warnings.append(str(exc))
    shares = combined
    if combined.empty or "total_share" not in combined or combined["total_share"].isna().all():
        try:
            shares = _dated(_fetch_range(client, "fund_share", start, end, token, ts_code=symbol))
            shares = shares.rename(columns={"fd_share": "total_share"})
        except DataProviderError as exc:
            errors.append(str(exc))
            shares = pd.DataFrame()
    # fund_nav also supplies the announcement date. Keep NAV and that date from
    # the same record, even when etf_share_size happens to return another NAV.
    need_nav = True
    nav = pd.DataFrame()
    nav_source, daily_source = "fund_nav", "fund_daily"
    if need_nav:
        try:
            nav = _dated(_fetch_range(client, "fund_nav", start, end, token, ts_code=symbol), "nav_date")
            nav = nav.rename(columns={"unit_nav": "nav", "ann_date": "nav_ann_date"})
        except DataProviderError as exc:
            errors.append(str(exc))
        if nav.empty and ENABLE_WEB_FALLBACK:
            try:
                web_nav = _web_fetch_etf_nav(symbol, start, end)
                if not web_nav.empty:
                    nav = web_nav
                    nav_source = "eastmoney_nav_first_seen"
                    warnings.append(f"{symbol} 净值使用互联网备用源；无原始公告日，按本次获取日期限制历史可见性。")
            except Exception:
                pass
    daily = pd.DataFrame()
    try:
        daily = _dated(_fetch_range(client, "fund_daily", start, end, token, ts_code=symbol))
    except DataProviderError as exc:
        errors.append(str(exc))
    if daily.empty and ENABLE_WEB_FALLBACK:
        try:
            web_daily = _web_fetch_etf_daily(symbol, start, end)
            if not web_daily.empty:
                daily = web_daily
                daily_source = "sina_web_kline"
                warnings.append(f"{symbol} 日线行情使用互联网开源接口备用补全。")
        except Exception:
            pass
    if not nav.empty:
        shares = shares.drop(columns=["nav"], errors="ignore")
    frames = []
    for frame in (shares, nav, daily):
        if frame.empty:
            continue
        if "ts_code" in frame and set(frame["ts_code"].dropna()) != {symbol}:
            errors.append(f"{symbol} 接口返回的基金代码与请求不一致，已拒绝这批记录。")
            continue
        frames.append(_slice(frame, start, end))
    frames = [frame for frame in frames if not frame.empty]
    if not frames:
        return _empty_observations(), errors, warnings
    dates = sorted({value for f in frames for value in f["trade_date"]})
    result = pd.DataFrame({"trade_date": dates}).set_index("trade_date")
    for frame in frames:
        for col in ("total_share", "nav", "close", "amount", "pct_chg", "nav_ann_date"):
            if col in frame:
                values = frame.set_index("trade_date")[col]
                if col in result:
                    result[col] = result[col].combine_first(values)
                else:
                    result[col] = values.reindex(result.index)
    result["ts_code"] = symbol
    result["source"] = f"etf_share_size+{daily_source}" if not combined.empty else f"fund_share+{nav_source}+{daily_source}"
    result = result.reset_index()
    for col in OBSERVATION_COLUMNS:
        if col not in result:
            result[col] = pd.NA
    for col in ("total_share", "nav", "close", "amount", "pct_chg"):
        result[col] = pd.to_numeric(result[col], errors="coerce")
        infinite = result[col].isin([float("inf"), -float("inf")])
        if infinite.any():
            warnings.append(f"{symbol} {col} 有非有限值，已标为缺失。")
            result.loc[infinite, col] = float("nan")
    # Invalid values remain explicit missing values, rather than manufacturing zero.
    for col in ("total_share", "nav", "close"):
        invalid = result[col].notna() & (result[col] <= 0)
        if invalid.any():
            warnings.append(f"{symbol} {col} 有 {int(invalid.sum())} 条非正数，已标为缺失。")
            result.loc[invalid, col] = float("nan")
    if result["pct_chg"].isna().all() and result["close"].notna().any():
        result["pct_chg"] = result["close"].pct_change(fill_method=None) * 100
        warnings.append(f"{symbol} 涨跌幅来自 ETF 收盘价计算，不代表基准指数。")
    if "nav_ann_date" in result:
        result["nav_ann_date"] = pd.to_datetime(result["nav_ann_date"].astype(str), errors="coerce", format="mixed").dt.strftime("%Y-%m-%d")
    return result[OBSERVATION_COLUMNS], errors, warnings


def _calendar_sessions(frame: pd.DataFrame, start: str, end: str, *, full: bool) -> list[str]:
    """Validate exchange adjacency, never guess sessions from weekdays."""
    if frame.empty or "is_open" not in frame:
        raise DataProviderError("交易日历为空或缺少开市标识，不能确认相邻交易日。")
    subset = _slice(frame, start, end)
    if full:
        expected = set(pd.date_range(start, end).strftime("%Y-%m-%d"))
        if set(subset["trade_date"]) != expected:
            raise DataProviderError("交易日历缺少日期，不能确认是否漏掉开市日。")
    flags = pd.to_numeric(subset["is_open"], errors="coerce")
    if not flags.isin([0, 1]).all():
        raise DataProviderError("交易日历包含无效开市标识。")
    opened = subset.loc[flags == 1].sort_values("trade_date")
    sessions = opened["trade_date"].tolist()
    if "pretrade_date" in opened:
        predecessors = pd.to_datetime(opened["pretrade_date"].astype(str), errors="coerce").dt.strftime("%Y-%m-%d").tolist()
        for index in range(1, len(sessions)):
            if predecessors[index] != sessions[index - 1]:
                raise DataProviderError("交易日历的上一交易日链条存在缺口，已拒绝跨日计算。")
    elif not full:
        raise DataProviderError("旧交易日历缺少上一交易日链条，需要重新获取完整日历。")
    return sessions


def fetch_market_data(start_date: str, end_date: str, force: bool = False) -> dict:
    """Return real ETF observations, SSE open dates, and JSON-safe provenance.

    A failed refresh keeps previously fetched values with an explicit stale-cache
    warning. Total unavailability raises DataProviderError. No synthetic data is
    produced, and no missing trading day is silently forward-filled.
    """
    start, end = _iso(start_date), _iso(end_date)
    if start > end:
        raise ValueError("开始日期不能晚于结束日期。")
    if (date.fromisoformat(end) - date.fromisoformat(start)).days > 3650:
        raise ValueError("单次查询最多支持十年，请缩短日期范围。")
    fetched_at = _now()
    meta: dict = {
        "requested_start": start, "requested_end": end, "fetched_at": fetched_at,
        "source": "local_tushare", "units": {"total_share": "万份", "nav": "元", "close": "元", "amount": "千元", "pct_chg": "%"},
        "errors": [], "warnings": [], "symbols": {}, "benchmark": [],
        "docs": DOCS, "cache_info": {},
    }
    cached = {etf["ts_code"]: _read_cache(etf["ts_code"]) for etf in ETF_UNIVERSE}
    auxiliary_cached = {key: _read_cache(key) for key in ("calendar", "benchmark")}
    need_live = any(not _cache_usable(m, start, end, force) for _, m in [*cached.values(), *auxiliary_cached.values()])
    calendar_old, calendar_old_meta = auxiliary_cached["calendar"]
    try:
        _calendar_sessions(calendar_old, start, end, full=calendar_old_meta.get("calendar_full", False))
    except DataProviderError:
        need_live = True
    client = None
    token = ""
    if need_live:
        try:
            client, token, credential_source = _client()
            meta["credential_source"] = credential_source
        except DataProviderError as exc:
            meta["errors"].append(str(exc))
    prefer_combined = False
    if client is not None:
        # Capability check once, rather than repeating an unsupported endpoint for every ETF.
        try:
            probe_start = max(start, (date.fromisoformat(end) - timedelta(days=14)).isoformat())
            probe = _fetch_range(client, "etf_share_size", probe_start, end, token,
                ts_code=ETF_UNIVERSE[0]["ts_code"], fields="trade_date,ts_code,total_share,nav,close")
            prefer_combined = not probe.empty and "total_share" in probe
            if not prefer_combined:
                meta["warnings"].append("etf_share_size 未返回份额数据，使用 fund_share + fund_nav + fund_daily。")
        except DataProviderError as exc:
            meta["warnings"].append(f"{exc}；已切换 fund_share + fund_nav + fund_daily。")
    collected: dict[str, pd.DataFrame] = {}

    def process(symbol: str) -> tuple[str, pd.DataFrame, dict]:
        old, old_meta = cached[symbol]
        detail: dict = {"errors": [], "warnings": []}
        if _cache_usable(old_meta, start, end, force):
            detail.update({"mode": "cache", "fetched_at": _range_updated_at(old_meta, start, end), "warnings": old_meta.get("source_warnings", [])[:]})
            return symbol, _slice(old, start, end), detail
        frame = _empty_observations()
        if client is not None:
            frame, detail["errors"], detail["warnings"] = _symbol(client, token, symbol, start, end, prefer_combined)
        valid_share = not frame.empty and frame["total_share"].notna().any()
        prior_slice = _slice(old, start, end)
        if detail["errors"] and not prior_slice.empty and prior_slice["total_share"].notna().any():
            detail.update({"mode": "stale_cache", "fetched_at": _range_updated_at(old_meta, start, end)})
            detail["warnings"].append(f"{symbol} 部分接口更新失败，保留 {old_meta.get('fetched_at', '未知日期')} 的旧缓存快照，避免混用新旧份额与净值。")
            return symbol, prior_slice, detail
        if valid_share:
            detail.update({"mode": "live", "fetched_at": fetched_at})
            frame = _replace_range(old, frame, start, end)
            source_warnings = list(dict.fromkeys(detail["warnings"] + meta["warnings"]))
            cache_meta = _manifest(old_meta, start, end, fetched_at, source_warnings=source_warnings)
            # An incomplete refresh must not acquire an apparently fresh cache TTL.
            if not detail["errors"]:
                _write_cache(symbol, frame, cache_meta)
            else:
                detail["mode"] = "partial_live"
            return symbol, _slice(frame, start, end), detail
        if not old.empty:
            detail.update({"mode": "stale_cache", "fetched_at": _range_updated_at(old_meta, start, end)})
            detail["warnings"].append(f"{symbol} 更新失败，正在使用旧缓存（{old_meta.get('fetched_at', '日期未知')}）。")
            return symbol, _slice(old, start, end), detail
        detail["mode"] = "unavailable"
        detail["errors"].append(f"{symbol} 在所选区间没有可用的真实份额记录。")
        return symbol, frame, detail

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(process, etf["ts_code"]) for etf in ETF_UNIVERSE]
        for future in as_completed(futures):
            symbol, frame, detail = future.result()
            collected[symbol] = frame
            meta["symbols"][symbol] = detail
            meta["cache_info"][symbol] = detail["mode"]
            meta["errors"].extend(f"{symbol}: {message}" for message in detail["errors"])
            meta["warnings"].extend(detail["warnings"])
    observations = pd.concat([collected[e["ts_code"]] for e in ETF_UNIVERSE], ignore_index=True)
    if observations.empty or not observations["total_share"].notna().any():
        reason = "；".join(meta["errors"][:3])
        raise DataProviderError("没有可用的 ETF 份额数据。" + reason)

    meta["auxiliary"] = {}

    def auxiliary(key: str, endpoint: str, **params: Any) -> pd.DataFrame:
        old, old_meta = auxiliary_cached[key]
        cache_valid = _covers(_segments(old_meta), start, end)
        if key == "calendar" and cache_valid:
            try:
                _calendar_sessions(old, start, end, full=old_meta.get("calendar_full", False))
            except DataProviderError as exc:
                meta["warnings"].append(str(exc))
                cache_valid = False
        if cache_valid and _cache_usable(old_meta, start, end, force):
            meta["cache_info"][key] = "cache"
            meta["auxiliary"][key] = {"mode": "cache", "fetched_at": _range_updated_at(old_meta, start, end)}
            return _slice(old, start, end)
        if client is not None:
            try:
                frame = _fetch_range(client, endpoint, start, end, token, **params)
                frame = _dated(frame, "cal_date" if endpoint == "trade_cal" else "trade_date")
                if frame.empty:
                    raise DataProviderError(f"{endpoint} 在查询区间没有返回记录。")
                frame = _slice(frame, start, end)
                if key == "calendar":
                    _calendar_sessions(frame, start, end, full=True)
                elif "ts_code" in frame and set(frame["ts_code"].dropna()) != {params["ts_code"]}:
                    raise DataProviderError("基准接口返回代码与请求不一致。")
                combined = _replace_range(old, frame, start, end)
                # Legacy open-day-only portions cannot become verified full
                # calendars merely because a smaller interval was refreshed.
                if key == "calendar" and not old_meta.get("calendar_full", False):
                    combined = frame
                    old_meta = {}
                _write_cache(key, combined, _manifest(old_meta, start, end, fetched_at,
                    calendar_full=key == "calendar"))
                meta["cache_info"][key] = "live"
                meta["auxiliary"][key] = {"mode": "live", "fetched_at": fetched_at}
                return frame
            except DataProviderError as exc:
                meta["warnings"].append(str(exc))
                if ENABLE_WEB_FALLBACK:
                    web_frame = pd.DataFrame()
                    if key == "benchmark":
                        web_frame = _web_fetch_benchmark(start, end)
                    elif key == "calendar":
                        web_frame = _web_fetch_calendar(start, end)
                    if not web_frame.empty:
                        try:
                            if key == "calendar":
                                _calendar_sessions(web_frame, start, end, full=True)
                            combined = _replace_range(old, web_frame, start, end)
                            _write_cache(key, combined, _manifest(old_meta, start, end, fetched_at,
                                calendar_full=key == "calendar"))
                            meta["cache_info"][key] = "live_web"
                            meta["auxiliary"][key] = {"mode": "live_web", "fetched_at": fetched_at}
                            meta["warnings"].append(f"{key} 从互联网开源数据备用获取成功。")
                            return web_frame
                        except Exception:
                            pass
        elif ENABLE_WEB_FALLBACK:
            web_frame = pd.DataFrame()
            if key == "benchmark":
                web_frame = _web_fetch_benchmark(start, end)
            elif key == "calendar":
                web_frame = _web_fetch_calendar(start, end)
            if not web_frame.empty:
                try:
                    if key == "calendar":
                        _calendar_sessions(web_frame, start, end, full=True)
                    combined = _replace_range(old, web_frame, start, end)
                    _write_cache(key, combined, _manifest(old_meta, start, end, fetched_at,
                        calendar_full=key == "calendar"))
                    meta["cache_info"][key] = "live_web"
                    meta["auxiliary"][key] = {"mode": "live_web", "fetched_at": fetched_at}
                    meta["warnings"].append(f"{key} 从互联网开源数据备用获取成功。")
                    return web_frame
                except Exception:
                    pass
        if not old.empty and (cache_valid or key == "benchmark"):
            meta["cache_info"][key] = "stale_cache"
            meta["auxiliary"][key] = {"mode": "stale_cache", "fetched_at": _range_updated_at(old_meta, start, end)}
            meta["warnings"].append(f"{key} 更新失败，采用 {old_meta.get('fetched_at', '未知日期')} 的缓存。")
            return _slice(old, start, end)
        meta["cache_info"][key] = "unavailable"
        meta["auxiliary"][key] = {"mode": "unavailable", "fetched_at": None}
        return pd.DataFrame()

    # Request closed days too. An open-day-only response cannot prove that a
    # missing date was a holiday instead of a dropped trading session.
    calendar_frame = auxiliary("calendar", "trade_cal", exchange="SSE")
    if calendar_frame.empty:
        raise DataProviderError("完整交易日历不可用，不能可靠识别相邻交易日；已保留原有缓存，请重试更新。")
    else:
        if "is_open" in calendar_frame:
            calendar_frame = calendar_frame[pd.to_numeric(calendar_frame["is_open"], errors="coerce") == 1]
        calendar = sorted(calendar_frame["trade_date"].unique().tolist())
    benchmark = auxiliary("benchmark", "index_daily", ts_code="000300.SH")
    if not benchmark.empty:
        keep = [column for column in ("trade_date", "close", "pct_chg") if column in benchmark]
        benchmark = benchmark[keep].copy()
        benchmark["source"] = "index_daily"
        meta["benchmark"] = json.loads(benchmark.to_json(orient="records", force_ascii=False))
    else:
        meta["warnings"].append("沪深300基准数据不可用，市场下跌辅助证据暂缺。")
    for etf in ETF_UNIVERSE:
        symbol = etf["ts_code"]
        frame = collected[symbol]
        detail = meta["symbols"][symbol]
        detail["rows"] = len(frame)
        detail["share_start"] = frame.loc[frame["total_share"].notna(), "trade_date"].min() if not frame.empty else None
        detail["share_end"] = frame.loc[frame["total_share"].notna(), "trade_date"].max() if not frame.empty else None
        detail["nav_end"] = frame.loc[frame["nav"].notna(), "trade_date"].max() if not frame.empty else None
        detail["missing_share_dates"] = sorted(set(calendar) - set(frame.loc[frame["total_share"].notna(), "trade_date"]))
        detail["missing_nav_dates"] = sorted(set(calendar) - set(frame.loc[frame["nav"].notna(), "trade_date"]))
        detail["non_trading_observation_dates"] = sorted(set(frame["trade_date"]) - set(calendar)) if calendar else []
        if detail["non_trading_observation_dates"]:
            meta["warnings"].append(f"{symbol} 有 {len(detail['non_trading_observation_dates'])} 条接口记录日期不在 SSE 开市日历中；计算相邻交易日时应排除。")
        for key in ("share_start", "share_end", "nav_end"):
            if pd.isna(detail[key]):
                detail[key] = None
    share_dates = observations.loc[observations["total_share"].notna(), "trade_date"]
    meta["data_start"] = share_dates.min()
    meta["data_end"] = share_dates.max()
    meta["calendar_source"] = "Tushare trade_cal SSE"
    meta["expected_latest_trade_date"] = calendar[-1] if calendar else None
    meta["is_stale"] = any(value == "stale_cache" for value in meta["cache_info"].values())
    missing_source = any(item.get("mode") == "unavailable" for item in meta["auxiliary"].values())
    meta["refresh_health"] = "stale" if meta["is_stale"] else ("partial" if meta["errors"] or missing_source or not meta["benchmark"] or "partial_live" in meta["cache_info"].values() else "ok")
    meta["warnings"] = list(dict.fromkeys(meta["warnings"]))
    return {"observations": observations.sort_values(["trade_date", "ts_code"]).reset_index(drop=True),
            "calendar": calendar, "metadata": meta}
