from datetime import date, datetime, timedelta

import pandas as pd
import pytest

from national_team_radar import provider
from national_team_radar.engine import analyze
from national_team_radar.universe import COMMON_LIST_DATE, ETF_UNIVERSE


def test_pool_start_is_latest_listing_not_inception_or_fixed_year_count():
    assert COMMON_LIST_DATE == "2016-11-04"
    assert COMMON_LIST_DATE == max(row["list_date"] for row in ETF_UNIVERSE)
    assert next(row for row in ETF_UNIVERSE if row["ts_code"] == "512100.SH")["found_date"] == "2016-09-29"


def test_provider_does_not_reject_twenty_years_before_attempting_data(tmp_path, monkeypatch):
    monkeypatch.setattr(provider, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(provider, "ENABLE_WEB_FALLBACK", False)
    def unavailable(): raise provider.DataProviderError("offline fixture")
    monkeypatch.setattr(provider, "_client", unavailable)
    with pytest.raises(provider.DataProviderError, match="没有可用"):
        provider.fetch_market_data("2005-02-23", "2026-10-16")


def test_long_history_refreshes_tail_without_redating_old_history():
    now = datetime.now(provider._SHANGHAI)
    old = (now - timedelta(days=90)).isoformat()
    fresh = now.isoformat()
    meta = provider._manifest({}, "2016-11-04", "2026-08-16", old)
    meta = provider._manifest(meta, "2026-08-17", "2026-10-16", fresh)
    assert provider._cache_usable(meta, "2016-11-04", "2026-10-16", False)
    assert provider._refresh_ranges(meta, "2016-11-04", "2026-10-16", False) == []
    assert provider._refresh_ranges(meta, "2016-11-04", "2026-10-16", True) == [("2026-08-17", "2026-10-16")]
    assert provider._latest_updated_at(meta, "2016-11-04", "2026-10-16") == fresh
    assert provider._range_updated_at(meta, "2016-11-04", "2026-10-16") == old


def test_expansion_backfills_missing_prefix_without_redownloading_fresh_tail():
    meta = provider._manifest({}, "2025-01-01", "2026-10-16", provider._now())
    assert provider._refresh_ranges(meta, "2016-11-04", "2026-10-16", False) == [("2016-11-04", "2024-12-31")]
    assert provider._refresh_ranges(meta, "2025-01-01", "2026-10-16", True) == [("2025-01-01", "2026-10-16")]


def test_long_refresh_preserves_history_and_only_requests_recent_symbol_data(tmp_path, monkeypatch):
    monkeypatch.setattr(provider, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(provider, "ETF_UNIVERSE", [ETF_UNIVERSE[0]])
    monkeypatch.setattr(provider, "ENABLE_WEB_FALLBACK", False)
    monkeypatch.setattr(provider, "_client", lambda: (object(), "", "test"))
    code = ETF_UNIVERSE[0]["ts_code"]
    def frame(days, amount):
        return pd.DataFrame([{**{c: None for c in provider.OBSERVATION_COLUMNS},
            "trade_date": d, "ts_code": code, "total_share": 1000000 + i,
            "nav": 4, "close": 4, "amount": amount, "pct_chg": 0,
            "source": "test", "nav_ann_date": d} for i, d in enumerate(days)])
    old_stamp = (datetime.now(provider._SHANGHAI) - timedelta(days=10)).isoformat()
    provider._write_cache(code, frame(["2016-11-04", "2026-10-15", "2026-10-16"], 1),
                          provider._manifest({}, "2016-11-04", "2026-10-16", old_stamp))
    calls = []
    def symbol(client, token, symbol, start, end, combined):
        calls.append((start, end))
        return frame(["2026-10-15", "2026-10-16"], 2), [], []
    monkeypatch.setattr(provider, "_symbol", symbol)
    def fetch(client, endpoint, start, end, token, **kwargs):
        if endpoint == "etf_share_size": raise provider.DataProviderError("unsupported")
        if endpoint == "trade_cal":
            days = pd.date_range(start, end)
            opened = [d.strftime("%Y-%m-%d") for d in days if d.dayofweek < 5]
            previous = {d: opened[i-1] if i else "2016-11-03" for i, d in enumerate(opened)}
            return pd.DataFrame([{"cal_date": d.strftime("%Y-%m-%d"), "is_open": int(d.dayofweek < 5),
                                  "pretrade_date": previous.get(d.strftime("%Y-%m-%d"), "")} for d in days])
        return pd.DataFrame([{"trade_date": "2016-11-04", "ts_code": "000300.SH", "close": 3300, "pct_chg": .1},
                             {"trade_date": "2026-10-16", "ts_code": "000300.SH", "close": 4200, "pct_chg": .1}])
    monkeypatch.setattr(provider, "_fetch_range", fetch)
    result = provider.fetch_market_data("2016-11-04", "2026-10-16", force=True)
    assert calls == [("2026-08-17", "2026-10-16")]
    assert result["observations"].iloc[0]["trade_date"] == "2016-11-04"
    assert result["observations"].iloc[0]["amount"] == 1
    assert result["observations"].iloc[-1]["amount"] == 2
    detail = result["metadata"]["symbols"][code]
    assert detail["history_oldest_fetched_at"] == old_stamp
    assert detail["fetched_at"] != old_stamp


def test_prelisting_creation_does_not_enter_postlisting_z_baseline():
    days = pd.bdate_range("2026-01-05", periods=10).strftime("%Y-%m-%d").tolist()
    rows = [{"trade_date": d, "ts_code": "TEST", "total_share": 1000000 + i * i * 100,
             "nav": 4, "close": 4} for i, d in enumerate(days)]
    result = analyze(rows, days, [{"ts_code": "TEST", "list_date": days[5]}], config={"window": 2})
    series = result["etf_series"]["TEST"]
    assert all(row["flow_yi"] is None for row in series[:5])
    assert "pre_listing_session" in series[4]["quality_codes"]
    assert series[5]["flow_yi"] is not None  # prior session supplies the anchor
    assert series[5]["z_score"] is None
    assert series[6]["z_score"] is None
    assert series[7]["z_score"] is not None
