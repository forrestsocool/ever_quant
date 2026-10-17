from datetime import timedelta
from unittest.mock import patch

from bs4 import BeautifulSoup
import pandas as pd

from national_team_radar import crosschecks, disclosures, provider
from national_team_radar.engine import _evidence
from national_team_radar.server import cn_now


def test_official_list_preserves_publication_and_first_seen_and_does_not_score():
    html = '''<div class="infor-list-item"><span class="day">07</span><span class="year">2025.04</span>
       <h1>公告</h1><p>已再次增持了交易型开放式指数基金，未来将继续增持。</p>
       <a href="/huijin-inv/SC20252/2025-04/1002841.shtml">查看更多</a></div>'''
    source = {"id": "huijin", "name": "中央汇金", "url": "https://www.huijin-inv.cn/list.shtml"}
    first_seen = cn_now().isoformat()
    with patch.object(disclosures, "_soup", return_value=BeautifulSoup(html, "html.parser")):
        rows, status = disclosures.collect_source(source, first_seen)
    assert status["state"] == "ok"
    assert rows[0]["publication_date"] == "2025-04-07"
    assert rows[0]["first_seen_at"] == first_seen
    assert rows[0]["action"] == "disclosed_increase"
    accepted, _ = _evidence(rows, "2025-04-07")
    assert accepted[0]["verification"] == "official_source"
    assert accepted[0]["affects_score"] is False
    rejected, _ = _evidence(rows, "2025-04-06")
    assert not rejected


def test_failed_official_feed_retains_items_but_reports_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(disclosures, "DATA", tmp_path)
    source = disclosures.SOURCES[0]
    monkeypatch.setattr(disclosures, "SOURCES", (source,))
    prior = {"items": [{"url": "https://www.huijin-inv.cn/old", "date": "2025-04-07", "first_seen_at": "2025-04-08"}]}
    monkeypatch.setattr(disclosures, "load_disclosures", lambda: prior)
    with patch.object(disclosures, "_soup", side_effect=TimeoutError()):
        result = disclosures.refresh_disclosures(force=True)
    assert result["items"] == prior["items"]
    assert result["sources"]["huijin"]["state"] == "unavailable"
    assert result["sources"]["huijin"]["error"] == "TimeoutError"


def test_holder_report_period_is_not_publication_date_and_absence_is_not_zero():
    frame = pd.DataFrame([
        {"ts_code": "601398.SH", "ann_date": "20260830", "end_date": "20260630", "holder_name": "中央汇金投资有限责任公司", "hold_amount": 100, "hold_ratio": 1},
        {"ts_code": "601398.SH", "ann_date": "20261030", "end_date": "20260930", "holder_name": "中央汇金投资有限责任公司", "hold_amount": 90, "hold_ratio": .9},
        {"ts_code": "601398.SH", "ann_date": "20260830", "end_date": "20260630", "holder_name": "普通机构", "hold_amount": 200},
    ])
    rows = crosschecks._rows(frame, "holders", "2026-10-17")
    assert len(rows) == 1
    assert rows[0]["date"] == "2026-08-30"
    assert rows[0]["report_period"] == "2026-06-30"
    assert rows[0]["hold_amount_shares"] == 100
    assert "delta" not in rows[0]


def test_transaction_units_and_institution_labels():
    blocks = crosschecks._rows(pd.DataFrame([{"ts_code": "600000.SH", "trade_date": "20261016", "price": 10, "vol": 1000, "buyer": "机构席位"}]), "block_trades", "2026-10-17")
    assert blocks[0]["amount_yi"] == 1
    assert blocks[0]["affects_score"] is False
    orders = crosschecks._rows(pd.DataFrame([{"ts_code": "600000.SH", "trade_date": "20261016", "buy_elg_amount": 20000, "sell_elg_amount": 5000, "net_mf_amount": 123}]), "large_orders", "2026-10-17")
    assert orders[0]["elg_active_difference_yi"] == 1.5
    assert orders[0]["vendor_net_mf_wan"] == 123  # Not reconstructed from sizes.
    seats = crosschecks._rows(pd.DataFrame([{"ts_code": "600000.SH", "trade_date": "20261016", "exalter": "机构专用", "net_buy": 1e8}, {"ts_code": "600000.SH", "trade_date": "20261016", "exalter": "营业部", "net_buy": 2e8}]), "dragon_tiger", "2026-10-17")
    assert len(seats) == 1
    assert seats[0]["net_buy_yuan"] == 1e8


def test_auxiliary_permission_failure_is_unavailable_not_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(crosschecks, "DATA", tmp_path)
    monkeypatch.setattr(crosschecks, "_client", lambda: (_ for _ in ()).throw(provider.DataProviderError("无权限")))
    result = crosschecks.refresh_crosschecks(["2026-10-16"], "2026-10-17", force=True)
    assert all(c["state"] == "unavailable" for c in result["channels"].values())
    assert all(c["rows"] == [] and c["errors"] for c in result["channels"].values())


def test_sina_volume_is_not_turnover_and_nav_uses_first_observed_date():
    class Response:
        status_code = 200
        def json(self):
            return [{"day": "2026-10-16", "close": "4", "volume": "10000000"}]
    with patch("requests.get", return_value=Response()):
        frame = provider._web_fetch_etf_daily("510300.SH", "2026-10-15", "2026-10-17")
    assert frame["amount"].isna().all()
    class NavResponse(Response):
        def json(self):
            return {"Data": {"LSJZList": [{"FSRQ": "2026-10-15", "DWJZ": "4"}]}}
    with patch("requests.get", return_value=NavResponse()), patch.object(provider, "_now", return_value="2026-10-17T10:00:00+08:00"):
        frame = provider._web_fetch_etf_nav("510300.SH", "2026-10-15", "2026-10-17")
    assert frame.iloc[0]["nav_ann_date"] == "2026-10-17"
    assert frame.iloc[0]["trade_date"] == "2026-10-15"
