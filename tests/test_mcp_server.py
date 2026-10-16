"""Exercise the MCP wire contract with the official SDK client."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import timedelta
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from mcp import Client
from mcp.client.stdio import StdioServerParameters

from national_team_radar import mcp_server
from national_team_radar.mcp_server import create_mcp_server
from national_team_radar.provider import ETF_UNIVERSE
from national_team_radar.server import cn_now


def fixture_snapshot(flow: float = 100.0, z: float = 3.0, sync_in: int = 3,
                     sync_out: int = 0) -> dict:
    day = cn_now().date()
    days = [(day - timedelta(days=2 - i)).isoformat() for i in range(3)]
    row = {"date": days[-1], "flow_yi": flow, "z_score": z, "positive_flow_yi": max(flow, 0),
           "negative_flow_yi": min(flow, 0), "valid_count": 7, "total_count": 7,
           "sync_in": sync_in, "sync_out": sync_out, "positive_count": 6,
           "negative_count": 1, "status": "大额净申购异动" if flow > 0 else "大额净赎回异动",
           "complete": True, "baseline_days": 20, "quality_codes": []}
    series = [{**row, "date": item} for item in days]
    benchmark = [{"trade_date": item, "close": 4200 + i, "pct_chg": -0.5,
                  "source": "index_daily"} for i, item in enumerate(days)]
    etf_series = {item["ts_code"]: [{"date": d, "flow_yi": 1.0, "z_score": 0.1,
                                      "total_share_yi": 10, "delta_share_yi": 0.01,
                                      "nav": 4.0, "quality_codes": [], "status": "观察"} for d in days]
                  for item in ETF_UNIVERSE}
    return {
        "schema_version": 1, "as_of": days[-1], "summary": dict(row), "last_complete": dict(row),
        "series": series, "etfs": [{**item, **etf_series[item["ts_code"]][-1]} for item in ETF_UNIVERSE],
        "etf_series": etf_series,
        "alerts": [{"date": days[-1], "direction": "in", "title": "大额净申购异动"}],
        "observation_events": [{"date": days[0], "direction": "out", "title": "同步观察"}],
        "evidence": [{"date": days[-1], "title": "人工线索", "kind": "market", "affects_score": False}],
        "quality": {"warnings": [], "coverage": {"valid_count": 7, "total_count": 7,
                    "missing_codes": [], "quarantined_codes": []},
                    "calendar_latest_session": days[-1], "latest_quote_date": days[-1]},
        "config": {"window": 20, "z_threshold": 2.0, "min_flow_yi": 10.0, "sync_min": 3},
        "metadata": {"view_start_date": days[0], "view_end_date": days[-1],
                     "benchmark": benchmark, "refresh_health": "ok",
                     "symbols": {ETF_UNIVERSE[0]["ts_code"]: {"mode": "live", "errors": ["token=secret"],
                                 "warnings": [], "fetched_at": "2026-10-16T09:00:00+08:00"}},
                     "auxiliary": {"benchmark": {"mode": "live", "fetched_at": "2026-10-16T09:00:00+08:00"}},
                     "historical_data_policy": "历史修订未重建"},
        "refresh": {"running": False, "degraded": False, "error": "token=secret",
                    "updated_at": "2026-10-16T09:00:00+08:00"},
        "sources": [{"title": "Tushare", "url": "https://tushare.pro"}],
        "method": {"formula": "(Q_t-Q_t-1)*NAV_t-1/10000", "units": {"flow_yi": "亿元"}},
        "benchmark": benchmark[-1],
    }


class FakeStore:
    def __init__(self, snapshot: dict):
        self.payload = snapshot

    def snapshot(self, config=None, start=None, end=None):
        result = deepcopy(self.payload)
        result["config"].update(config or {})
        if start:
            result["metadata"]["view_start_date"] = start
        if end:
            result["metadata"]["view_end_date"] = end
        return result


class MCPServerTests(unittest.IsolatedAsyncioTestCase):
    async def test_client_handshake_tools_and_resources(self):
        async with Client(create_mcp_server(FakeStore(fixture_snapshot()))) as client:
            tools = await client.list_tools()
            names = {tool.name for tool in tools.tools}
            self.assertTrue({"get_radar_overview", "get_market_timing_brief", "get_daily_series",
                             "get_etf_detail", "get_historical_events", "get_data_quality",
                             "get_methodology", "list_etf_universe"} <= names)
            universe = await client.call_tool("list_etf_universe", {})
            self.assertFalse(universe.is_error)
            self.assertEqual(len(universe.structured_content["etfs"]), 7)
            resources = await client.list_resources()
            self.assertIn("radar://overview", {str(item.uri) for item in resources.resources})
            result = await client.read_resource("radar://methodology")
            self.assertIn("formula", result.contents[0].text)
            templates = await client.list_resource_templates()
            self.assertIn("radar://etf/{ts_code}", {str(item.uri_template) for item in templates.resource_templates})
            etf_resource = await client.read_resource("radar://etf/510300.SH")
            self.assertIn("510300.SH", etf_resource.contents[0].text)

    async def test_timing_requires_complete_fresh_and_index(self):
        snap = fixture_snapshot()
        async with Client(create_mcp_server(FakeStore(snap))) as client:
            result = (await client.call_tool("get_market_timing_brief", {})).structured_content
            self.assertEqual(result["decision_state"], "insufficient_data")  # refresh error in fixture
            self.assertFalse(result["entry_support"]["current_session_usable"])
        snap["refresh"]["error"] = None
        async with Client(create_mcp_server(FakeStore(snap))) as client:
            result = (await client.call_tool("get_market_timing_brief", {})).structured_content
            self.assertEqual(result["decision_state"], "support_watch")
            self.assertTrue(all(result["entry_support"]["checks"].values()))
            self.assertFalse(result["exit_pressure"]["current_session_usable"])
            self.assertEqual(result["csi300"]["trade_date"], result["evaluated_session"])
            stricter = (await client.call_tool("get_market_timing_brief", {"z_threshold": 4.0})).structured_content
            self.assertEqual(stricter["decision_state"], "neutral")
        no_index = deepcopy(snap)
        no_index["metadata"]["benchmark"] = []
        async with Client(create_mcp_server(FakeStore(no_index))) as client:
            result = (await client.call_tool("get_market_timing_brief", {})).structured_content
            self.assertEqual(result["decision_state"], "insufficient_data")
        snap["quality"]["calendar_latest_session"] = (cn_now().date() + timedelta(days=1)).isoformat()
        async with Client(create_mcp_server(FakeStore(snap))) as client:
            result = (await client.call_tool("get_market_timing_brief", {})).structured_content
            self.assertEqual(result["decision_state"], "insufficient_data")
            self.assertEqual(result["entry_support"]["state"], "triggered_historical")

    async def test_default_latest_cache_is_not_a_historical_cutoff_on_weekends(self):
        snap = fixture_snapshot()
        last_exchange_day = (cn_now().date() - timedelta(days=7)).isoformat()
        snap["as_of"] = last_exchange_day
        snap["summary"]["date"] = last_exchange_day
        snap["last_complete"]["date"] = last_exchange_day
        snap["quality"]["calendar_latest_session"] = last_exchange_day
        snap["metadata"]["view_end_date"] = last_exchange_day
        snap["metadata"]["benchmark"][-1]["trade_date"] = last_exchange_day
        snap["refresh"]["error"] = None
        async with Client(create_mcp_server(FakeStore(snap))) as client:
            latest = (await client.call_tool("get_market_timing_brief", {})).structured_content
            self.assertFalse(latest["context"]["historical_cutoff"])
            self.assertEqual(latest["decision_state"], "support_watch")
            explicit_last = (await client.call_tool("get_market_timing_brief", {"end_date": last_exchange_day})).structured_content
            self.assertFalse(explicit_last["context"]["historical_cutoff"])

    async def test_exit_watch_divergence_and_historical_cutoff(self):
        snap = fixture_snapshot(-100, -3, 0, 3)
        snap["refresh"]["error"] = None
        async with Client(create_mcp_server(FakeStore(snap))) as client:
            result = (await client.call_tool("get_market_timing_brief", {})).structured_content
            self.assertEqual(result["decision_state"], "exit_watch")
            self.assertTrue(result["exit_pressure"]["current_session_usable"])
            historical = (await client.call_tool("get_market_timing_brief", {
                "end_date": (cn_now().date() - timedelta(days=1)).isoformat()})).structured_content
            self.assertEqual(historical["decision_state"], "insufficient_data")
            self.assertTrue(historical["context"]["historical_cutoff"])
        snap["last_complete"]["status"] = "观察"
        snap["last_complete"]["sync_out"] = 2
        async with Client(create_mcp_server(FakeStore(snap))) as client:
            result = (await client.call_tool("get_market_timing_brief", {})).structured_content
            self.assertEqual(result["decision_state"], "divergence")
        snap["last_complete"].update(flow_yi=0, z_score=0, sync_out=0)
        snap["observation_events"][0]["date"] = snap["last_complete"]["date"]
        async with Client(create_mcp_server(FakeStore(snap))) as client:
            result = (await client.call_tool("get_market_timing_brief", {})).structured_content
            self.assertEqual(result["decision_state"], "divergence")
            self.assertIn("同步", result["divergence_note"])

    async def test_series_paging_events_and_quality_redaction(self):
        async with Client(create_mcp_server(FakeStore(fixture_snapshot()))) as client:
            result = (await client.call_tool("get_daily_series", {"limit": 1, "offset": 1})).structured_content
            self.assertEqual(result["returned"], 1)
            self.assertEqual(result["next_offset"], 2)
            self.assertEqual(result["rows"][0]["csi300_close"], 4201)
            etf = (await client.call_tool("get_etf_detail", {"ts_code": "510300.SH", "limit": 1})).structured_content
            self.assertEqual(etf["returned"], 1)
            events = (await client.call_tool("get_historical_events", {"limit": 1})).structured_content
            self.assertEqual(events["returned"], 1)
            self.assertIsNotNone(events["next_offset"])
            quality = (await client.call_tool("get_data_quality", {})).structured_content
            self.assertNotIn("token=secret", str(quality))
            invalid = await client.call_tool("get_daily_series", {"limit": 121})
            self.assertTrue(invalid.is_error)
            unknown = await client.call_tool("get_etf_detail", {"ts_code": "000000.SH"})
            self.assertTrue(unknown.is_error)

    async def test_real_stdio_handshake_and_tool_call(self):
        root = Path(__file__).resolve().parents[1]
        params = StdioServerParameters(command=sys.executable,
                                       args=["-m", "national_team_radar.mcp_server"],
                                       cwd=root)
        async def exercise():
            async with Client(params) as client:
                self.assertIn("list_etf_universe", {tool.name for tool in (await client.list_tools()).tools})
                result = await client.call_tool("list_etf_universe", {})
                self.assertFalse(result.is_error)
                self.assertEqual(len(result.structured_content["etfs"]), 7)
        await asyncio.wait_for(exercise(), timeout=30)

    async def test_long_lived_server_reloads_atomic_cache_replace(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "market_data.json"
            path.write_text(json.dumps(fixture_snapshot()), encoding="utf-8")

            class DiskStore(FakeStore):
                def __init__(self):
                    self.lock = threading.RLock()
                    self.load()

                def load(self):
                    self.payload = json.loads(path.read_text(encoding="utf-8"))

            with patch.object(mcp_server, "DATA", Path(temporary)), patch.object(mcp_server, "RadarStore", DiskStore):
                server = create_mcp_server()
            async with Client(server) as client:
                before = (await client.call_tool("get_radar_overview", {})).structured_content
                self.assertEqual(before["last_complete"]["flow_yi"], 100)
                changed = fixture_snapshot(-60, -2.5, 0, 3)
                replacement = Path(temporary) / "next.json"
                replacement.write_text(json.dumps(changed), encoding="utf-8")
                os.replace(replacement, path)
                after = (await client.call_tool("get_radar_overview", {})).structured_content
                self.assertEqual(after["last_complete"]["flow_yi"], -60)


if __name__ == "__main__":
    unittest.main()
