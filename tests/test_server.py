from copy import deepcopy
from datetime import date, datetime, timedelta
import csv
import io
import json

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from national_team_radar import server


@pytest.fixture
def configured(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "DATA", tmp_path)
    monkeypatch.setattr(server, "load_evidence", lambda: [])
    monkeypatch.setattr(server, "refresh_cross_validation", lambda *args: None)
    days = pd.bdate_range("2026-06-01", periods=65).strftime("%Y-%m-%d").tolist()
    rows = []
    for code in server.ETF_UNIVERSE:
        total = 2_000_000
        for i, day in enumerate(days):
            total += 500 if i % 2 else -100
            rows.append({"trade_date": day, "ts_code": code["ts_code"], "total_share": total, "nav": 4, "close": 4.02, "pct_chg": 0.2, "amount": 1000})
    store = server.RadarStore(days[0], days[-1])
    store.data = {"observations": pd.DataFrame(rows), "calendar": days, "metadata": {"fetched_at": "2026-09-01T10:00:00+08:00", "requested_start_date": days[0], "requested_end_date": days[-1]}}
    return store, days


def test_api_snapshot_csv_and_threshold_validation(configured):
    store, days = configured
    with TestClient(server.create_app(store, auto_update=False, initial_refresh=False)) as client:
        snapshot = client.get("/api/snapshot")
        assert snapshot.status_code == 200
        assert snapshot.json()["as_of"] == days[-1]
        assert snapshot.json()["summary"]["valid_count"] == 7
        assert snapshot.json()["sources"]
        csv = client.get("/api/export.csv")
        assert csv.status_code == 200
        assert "flow_yi" in csv.text
        assert "510300.SH" in csv.text
        assert "Content-Disposition" in csv.headers
        assert client.get("/api/snapshot?min_flow_yi=0").status_code == 422
        assert client.get("/api/snapshot?window=1").status_code == 422


def test_historical_query_does_not_borrow_future_values(configured):
    store, days = configured
    before = store.snapshot(end=days[40])
    store.data["observations"].loc[store.data["observations"]["trade_date"] > days[40], "total_share"] = 99_000_000
    after = store.snapshot(end=days[40])
    assert before["summary"] == after["summary"]
    assert after["as_of"] == days[40]
    assert all(row["date"] <= days[40] for row in after["series"])


def test_local_mutation_rejects_cross_site(configured):
    store, _ = configured
    with TestClient(server.create_app(store, auto_update=False, initial_refresh=False)) as client:
        assert client.post("/api/refresh", json={}, headers={"Origin": "https://unrelated.example"}).status_code == 403
        assert client.get("/api/config", headers={"Host": "unrelated.example"}).status_code == 403
        assert client.get("/api/config", headers={"Host": "evil.radar.sensen.li"}).status_code == 403


def test_remote_mcp_is_mounted_and_refresh_has_global_cooldown(configured):
    store, _ = configured
    store.state["last_attempt_at"] = server.cn_now().isoformat()
    with TestClient(server.create_app(store, auto_update=False, initial_refresh=False)) as client:
        assert client.post("/api/refresh", json={}).status_code == 429
        response = client.post("/mcp", headers={"Accept": "application/json, text/event-stream"}, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}})
        assert response.status_code == 200
        assert response.json()["result"]["serverInfo"]["name"] == "national-team-etf-flow-radar"


def test_failed_refresh_keeps_last_data(configured, monkeypatch):
    store, _ = configured
    prior = store.data
    def fail(*args, **kwargs):
        raise ConnectionError("transport internals should not reach the browser")
    monkeypatch.setattr(server, "fetch_market_data", fail)
    store._fetch(*store.defaults, force=True)
    assert store.data is prior
    assert store.state["error"]
    assert "transport internals" not in store.state["error"]
    assert not store.state["running"]


def test_invalid_and_future_ranges():
    with pytest.raises(ValueError):
        server.parse_dates("2026-09-01", "2026-08-01")
    with pytest.raises(ValueError):
        server.parse_dates(None, (server.cn_now().date() + timedelta(days=1)).isoformat())


def test_json_never_emits_non_finite_values(tmp_path):
    target = tmp_path / "snapshot.json"
    server.atomic_json(target, {"flow": float("nan"), "z": float("inf")})
    assert json.loads(target.read_text(encoding="utf-8")) == {"flow": None, "z": None}


def test_historical_cutoff_masks_unannounced_nav_and_nested_benchmark(configured):
    store, days = configured
    frame = store.data["observations"]
    frame["nav_ann_date"] = frame["trade_date"]
    frame.loc[frame["trade_date"] == days[39], "nav_ann_date"] = days[41]
    store.data["metadata"]["benchmark"] = [{"trade_date": d, "close": 100} for d in days]
    snapshot = store.snapshot(end=days[40])
    assert snapshot["summary"]["flow_yi"] is None
    assert snapshot["metadata"]["navs_excluded_after_cutoff"] == 7
    assert all(x["trade_date"] <= days[40] for x in snapshot["metadata"]["benchmark"])


def test_csv_matches_nondefault_dashboard_window(configured):
    store, _ = configured
    with TestClient(server.create_app(store, auto_update=False, initial_refresh=False)) as client:
        snapshot = client.get("/api/snapshot?window=5&min_flow_yi=1").json()
        response = client.get("/api/export.csv?window=5&min_flow_yi=1")
        rows = list(csv.DictReader(io.StringIO(response.text.lstrip("\ufeff"))))
        expected = next(row for row in snapshot["etf_series"]["510300.SH"] if row["z_score"] is not None)
        actual = next(row for row in rows if row["ts_code"] == "510300.SH" and row["date"] == expected["date"])
        assert float(actual["z_score"]) == expected["z_score"]


def test_short_refresh_fetches_extra_warmup_history(configured, monkeypatch):
    store, days = configured
    calls = []
    def fetch(begin, finish, force=False):
        calls.append((begin, finish))
        return store.data
    monkeypatch.setattr(server, "fetch_market_data", fetch)
    store._fetch(days[40], days[-1], False)
    assert calls[0][0] < days[0]
    assert store.data["metadata"]["requested_start_date"] == days[40]
    assert store.data["metadata"]["fetched_start_date"] == calls[0][0]


def test_range_not_fetched_is_explicitly_flagged(configured):
    store, days = configured
    before = (date.fromisoformat(days[0]) - timedelta(days=365)).isoformat()
    snapshot = store.snapshot(start=before)
    assert any("未覆盖" in text for text in snapshot["quality"]["warnings"])


def test_invalid_candidate_does_not_replace_memory_or_persisted_market(configured, monkeypatch):
    store, _ = configured
    prior = store.data
    target = server.DATA / "market_data.json"
    target.write_text("previous valid market", encoding="utf-8")
    candidate = {**prior, "calendar": []}
    monkeypatch.setattr(server, "fetch_market_data", lambda *args, **kwargs: candidate)
    store._fetch(*store.defaults, force=True)
    assert store.data is prior
    assert target.read_text(encoding="utf-8") == "previous valid market"
    assert "交易日历" in store.state["error"]


def test_analysis_failure_keeps_prior_data_even_when_download_succeeds(configured, monkeypatch):
    store, _ = configured
    prior = store.data
    monkeypatch.setattr(server, "fetch_market_data", lambda *args, **kwargs: deepcopy(prior))
    def fail(*args, **kwargs):
        raise ValueError("candidate is invalid")
    monkeypatch.setattr(server, "analyze", fail)
    store._fetch(*store.defaults, force=True)
    assert store.data is prior
    assert not (server.DATA / "market_data.json").exists()
    assert store.state["error"]


def test_persisted_snapshot_never_claims_refresh_is_still_running(configured, monkeypatch):
    store, _ = configured
    source = deepcopy(store.data)
    monkeypatch.setattr(server, "fetch_market_data", lambda *args, **kwargs: deepcopy(source))
    store.state["running"] = True
    store._fetch(*store.defaults, force=True)
    assert not store.state["running"]
    snapshot = json.loads((server.DATA / "snapshot.json").read_text(encoding="utf-8"))
    assert snapshot["refresh"]["running"] is False
    assert store.state["error"] is None


def test_degraded_refresh_uses_real_source_time_and_explicit_status(configured, monkeypatch):
    store, _ = configured
    source = deepcopy(store.data)
    source["metadata"].update(
        fetched_at="2026-10-15T18:30:00+08:00", is_stale=True,
        symbols={"510300.SH": {"mode": "stale_cache", "fetched_at": "2026-10-14T09:00:00+08:00"}},
    )
    monkeypatch.setattr(server, "fetch_market_data", lambda *args, **kwargs: source)
    store._fetch(*store.defaults, force=True)
    assert store.state["degraded"]
    assert store.state["warning"]
    assert "旧缓存" in store.state["stage"]
    assert store.state["updated_at"] == "2026-10-14T09:00:00+08:00"
    assert store.data["metadata"]["refresh_health"] == "stale"


def test_manual_refresh_preserves_selected_range_by_default(configured, monkeypatch):
    store, days = configured
    calls = []
    class Thread:
        def __init__(self, **kwargs):
            calls.append(kwargs)
        def start(self):
            pass
    monkeypatch.setattr(server.threading, "Thread", Thread)
    assert store.refresh()
    assert calls[0]["args"][:2] == (days[0], days[-1])
    assert not store.refresh()
    assert len(calls) == 1


def test_scheduler_catches_up_after_busy_slot_and_preserves_start(configured, monkeypatch):
    store, days = configured
    calls = []
    outcomes = iter([False, True, True])
    def refresh(**kwargs):
        calls.append(kwargs)
        return next(outcomes)
    monkeypatch.setattr(store, "refresh", refresh)
    assert not store.scheduler_tick(datetime(2026, 10, 15, 9, 0, tzinfo=server.CN))
    assert store.scheduler_tick(datetime(2026, 10, 15, 9, 3, tzinfo=server.CN))
    assert not store.scheduler_tick(datetime(2026, 10, 15, 9, 4, tzinfo=server.CN))
    assert store.scheduler_tick(datetime(2026, 10, 15, 19, 0, tzinfo=server.CN))
    assert calls[0]["start"] == days[0]
    assert calls[0]["end"] == "2026-10-15"


def test_restart_restores_last_range_but_explicit_cli_range_wins(configured):
    store, days = configured
    raw = {**store.data, "observations": store.data["observations"].to_dict(orient="records")}
    server.atomic_json(server.DATA / "market_data.json", raw)
    loaded = server.RadarStore()
    assert loaded.defaults == (days[0], days[-1])
    explicit = server.RadarStore(days[10], days[20])
    assert explicit.defaults == (days[10], days[20])


def test_pandas_missing_scalars_are_serialized_as_null(tmp_path):
    target = tmp_path / "missing.json"
    server.atomic_json(target, {"nav": pd.NA, "date": pd.NaT})
    assert json.loads(target.read_text(encoding="utf-8")) == {"nav": None, "date": None}


def test_incomplete_cache_is_rejected_before_publishing_in_memory(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "DATA", tmp_path)
    server.atomic_json(tmp_path / "market_data.json", {
        "observations": [{"trade_date": "2026-10-14", "total_share": 100}],
        "calendar": ["2026-10-14"],
    })
    store = server.RadarStore()
    assert store.data is None
    assert "快照损坏" in store.state["error"]
    with pytest.raises(ValueError, match="还没有本地数据"):
        store.snapshot()


def test_observation_events_are_trimmed_to_the_selected_view(configured, monkeypatch):
    store, days = configured
    original = server.analyze
    def analyze(*args, **kwargs):
        result = original(*args, **kwargs)
        result["observation_events"] = [{"date": days[1]}, {"date": days[40]}]
        return result
    monkeypatch.setattr(server, "analyze", analyze)
    snapshot = store.snapshot(start=days[30], end=days[45])
    assert snapshot["observation_events"] == [{"date": days[40]}]


def test_allowed_hosts_domain_support(configured, monkeypatch):
    store, _ = configured
    # Test wildcard (default)
    monkeypatch.setenv("ALLOWED_HOSTS", "*")
    app = server.create_app(store, auto_update=False, initial_refresh=False)
    with TestClient(app) as client:
        res = client.get("/api/status", headers={"Host": "radar.sensen.li"})
        assert res.status_code == 200

    # Test domain whitelist
    monkeypatch.setenv("ALLOWED_HOSTS", "radar.sensen.li,.sensen.li")
    app2 = server.create_app(store, auto_update=False, initial_refresh=False)
    with TestClient(app2) as client:
        assert client.get("/api/status", headers={"Host": "radar.sensen.li"}).status_code == 200
        assert client.get("/api/status", headers={"Host": "sub.sensen.li"}).status_code == 200
        assert client.get("/api/status", headers={"Host": "evil.example.com"}).status_code == 403

