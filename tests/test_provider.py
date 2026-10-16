import json
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import pandas as pd

from national_team_radar import provider


class FakeClient:
    def __init__(self):
        self.calls = []
        self.offline = False

    def __getattr__(self, endpoint):
        def call(**params):
            self.calls.append((endpoint, params))
            if self.offline:
                raise RuntimeError("offline token=secret-test-token-should-not-leak")
            code = params.get("ts_code", "510300.SH")
            if endpoint == "etf_share_size":
                raise RuntimeError("接口不存在 etf_share_size")
            if endpoint == "fund_share":
                return pd.DataFrame([
                    {"ts_code": code, "trade_date": "20261013", "fd_share": 50000.0},
                    {"ts_code": code, "trade_date": "20261014", "fd_share": 53000.0},
                ])
            if endpoint == "fund_nav":
                # The latest share date intentionally has no published NAV.
                return pd.DataFrame([
                    {"ts_code": code, "nav_date": "20261013", "ann_date": "20261014", "unit_nav": 4.2},
                ])
            if endpoint == "fund_daily":
                return pd.DataFrame([
                    {"ts_code": code, "trade_date": "20261013", "close": 4.21, "amount": 12345.0, "pct_chg": 1.0},
                    {"ts_code": code, "trade_date": "20261014", "close": 4.25, "amount": 14567.0, "pct_chg": 0.95},
                    {"ts_code": code, "trade_date": "20261015", "close": 4.1, "amount": 22345.0, "pct_chg": -3.5},
                ])
            if endpoint == "trade_cal":
                return pd.DataFrame([
                    {"cal_date": "20261013", "is_open": 1},
                    {"cal_date": "20261014", "is_open": 1},
                    {"cal_date": "20261015", "is_open": 1},
                ])
            if endpoint == "index_daily":
                return pd.DataFrame([
                    {"ts_code": code, "trade_date": "20261014", "close": 4217.0, "pct_chg": -1.35},
                ])
            raise AssertionError(endpoint)
        return call


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.client = FakeClient()
        self.patches = [
            patch.object(provider, "CACHE_DIR", Path(self.directory.name)),
            patch.object(provider, "_client", return_value=(self.client, "secret-test-token-should-not-leak", "test")),
            patch.object(provider, "ETF_UNIVERSE", [provider.ETF_UNIVERSE[0]]),
            patch.object(provider.time, "sleep"),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def test_fallback_preserves_units_and_missing_disclosures(self):
        data = provider.fetch_market_data("2026-10-13", "2026-10-15", force=True)
        frame = data["observations"].set_index("trade_date")
        self.assertEqual(frame.loc["2026-10-14", "total_share"], 53000)
        self.assertEqual(frame.loc["2026-10-13", "nav"], 4.2)
        self.assertTrue(pd.isna(frame.loc["2026-10-14", "nav"]))
        self.assertTrue(pd.isna(frame.loc["2026-10-15", "total_share"]))
        self.assertEqual(frame.loc["2026-10-15", "amount"], 22345)
        self.assertEqual(data["calendar"], ["2026-10-13", "2026-10-14", "2026-10-15"])
        self.assertEqual(data["metadata"]["benchmark"][0]["pct_chg"], -1.35)
        self.assertEqual(data["metadata"]["units"]["total_share"], "万份")
        self.assertEqual(data["metadata"]["symbols"]["510300.SH"]["missing_share_dates"], ["2026-10-15"])
        json.dumps(data["metadata"], ensure_ascii=False, allow_nan=False)

    def test_cache_reuses_complete_query_and_failed_refresh_marks_stale(self):
        provider.fetch_market_data("20261013", "20261015", force=True)
        calls = len(self.client.calls)
        result = provider.fetch_market_data("20261013", "20261015")
        self.assertEqual(len(self.client.calls), calls)
        self.assertEqual(result["metadata"]["cache_info"]["510300.SH"], "cache")
        self.client.offline = True
        result = provider.fetch_market_data("20261013", "20261015", force=True)
        self.assertTrue(result["metadata"]["is_stale"])
        self.assertEqual(result["metadata"]["cache_info"]["510300.SH"], "stale_cache")
        self.assertTrue(any("旧缓存" in text for text in result["metadata"]["warnings"]))
        serialized = json.dumps(result["metadata"], ensure_ascii=False)
        self.assertNotIn("secret-test-token-should-not-leak", serialized)

    def test_all_unavailable_raises_sanitized_error(self):
        self.client.offline = True
        with self.assertRaises(provider.DataProviderError) as context:
            provider.fetch_market_data("20261013", "20261015", force=True)
        self.assertIn("没有可用", str(context.exception))
        self.assertNotIn("secret-test-token-should-not-leak", str(context.exception))

    def test_row_limit_splits_date_range_instead_of_truncating(self):
        class Capped:
            def fund_share(self, **params):
                dates = pd.date_range(params["start_date"], params["end_date"])
                return pd.DataFrame({"trade_date": [d.strftime("%Y%m%d") for d in dates[:2]]})
        with patch.dict(provider._LIMITS, {"fund_share": 2}):
            result = provider._fetch_range(Capped(), "fund_share", "2026-01-01", "2026-01-05", "")
        self.assertEqual(set(result["trade_date"]), {"20260101", "20260102", "20260103", "20260104", "20260105"})

    def test_credentials_ast_read_never_executes_strategy(self):
        local_root = Path(self.directory.name) / "project"
        local_root.mkdir()
        (local_root / "backtest.py").write_text(
            "raise RuntimeError('must never execute')\nts.set_token('example-local-secret')\n",
            encoding="utf-8",
        )
        with patch.object(provider, "ROOT", local_root), patch.object(provider.Path, "home", return_value=local_root), patch.dict(provider.os.environ, {}, clear=True):
            token, source = provider._credential()
        self.assertEqual(token, "example-local-secret")
        self.assertEqual(source, "local_strategy_literal")

    def test_invalid_and_reversed_dates_rejected_before_client(self):
        for start, end in [("bad", "20261015"), ("20261016", "20261015")]:
            with self.assertRaises(ValueError):
                provider.fetch_market_data(start, end)

    def test_successful_missing_nav_replaces_old_cache_without_backfill(self):
        complete_nav = pd.DataFrame([
            {"ts_code": "510300.SH", "nav_date": "20261013", "ann_date": "20261014", "unit_nav": 4.2},
            {"ts_code": "510300.SH", "nav_date": "20261014", "ann_date": "20261015", "unit_nav": 4.3},
        ])
        with patch.object(self.client, "fund_nav", return_value=complete_nav):
            provider.fetch_market_data("20261013", "20261015", force=True)
        result = provider.fetch_market_data("20261013", "20261015", force=True)
        day = result["observations"].set_index("trade_date").loc["2026-10-14"]
        self.assertTrue(pd.isna(day["nav"]))
        self.assertTrue(pd.isna(day["nav_ann_date"]))
        cached, _ = provider._read_cache("510300.SH")
        self.assertTrue(pd.isna(cached.set_index("trade_date").loc["2026-10-14", "nav"]))

    def test_partial_endpoint_failure_keeps_consistent_old_snapshot(self):
        first = provider.fetch_market_data("20261013", "20261015", force=True)
        with patch.object(self.client, "fund_nav", side_effect=RuntimeError("network unavailable")), patch.object(
            self.client, "fund_share", return_value=pd.DataFrame([
                {"ts_code": "510300.SH", "trade_date": "20261014", "fd_share": 9000000.0}
            ])
        ):
            result = provider.fetch_market_data("20261013", "20261015", force=True)
        self.assertEqual(result["metadata"]["cache_info"]["510300.SH"], "stale_cache")
        self.assertEqual(result["metadata"]["refresh_health"], "stale")
        pd.testing.assert_frame_equal(result["observations"], first["observations"], check_dtype=False)

    def test_expired_auxiliary_cache_initializes_client_even_if_etfs_are_fresh(self):
        provider.fetch_market_data("20261013", "20261015", force=True)
        path = provider.CACHE_DIR / "benchmark.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        for item in manifest["ranges"]:
            item["fetched_at"] = "2000-01-01T00:00:00+08:00"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        provider._client.reset_mock()
        before = len([item for item in self.client.calls if item[0] == "fund_share"])
        result = provider.fetch_market_data("20261013", "20261015")
        provider._client.assert_called_once()
        self.assertEqual(len([item for item in self.client.calls if item[0] == "fund_share"]), before)
        self.assertEqual(result["metadata"]["cache_info"]["benchmark"], "live")

    def test_short_refresh_retains_auxiliary_history_and_provenance(self):
        provider.fetch_market_data("20261013", "20261015", force=True)
        provider.fetch_market_data("20261014", "20261014", force=True)
        frame, manifest = provider._read_cache("calendar")
        self.assertEqual(frame["trade_date"].tolist(), ["2026-10-13", "2026-10-14", "2026-10-15"])
        self.assertTrue(provider._covers(provider._segments(manifest), "2026-10-13", "2026-10-15"))
        result = provider.fetch_market_data("20261013", "20261015")
        self.assertTrue(any("etf_share_size" in text for text in result["metadata"]["warnings"]))

    def test_disjoint_ranges_do_not_claim_unfetched_gap_or_refresh_old_prefix(self):
        now = provider._now()
        old = (datetime.fromisoformat(now) - timedelta(days=3)).isoformat()
        first = provider._manifest({}, "2026-01-01", "2026-01-05", old)
        manifest = provider._manifest(first, "2026-01-10", "2026-01-15", now)
        self.assertFalse(provider._covers(provider._segments(manifest), "2026-01-01", "2026-01-15"))
        self.assertTrue(provider._cache_usable(manifest, "2026-01-10", "2026-01-15", False))
        self.assertFalse(provider._cache_usable(manifest, "2026-01-01", "2026-01-05", False))
        self.assertEqual(provider._range_updated_at(manifest, "2026-01-01", "2026-01-05"), old)

    def test_incomplete_calendar_falls_back_without_overwriting_good_cache(self):
        provider.fetch_market_data("20261013", "20261015", force=True)
        missing_day = pd.DataFrame([
            {"cal_date": "20261013", "is_open": 1},
            {"cal_date": "20261015", "is_open": 1},
        ])
        with patch.object(self.client, "trade_cal", return_value=missing_day):
            result = provider.fetch_market_data("20261013", "20261015", force=True)
        self.assertEqual(result["calendar"], ["2026-10-13", "2026-10-14", "2026-10-15"])
        self.assertEqual(result["metadata"]["cache_info"]["calendar"], "stale_cache")
        self.assertTrue(any("日历缺少" in text for text in result["metadata"]["warnings"]))
        cached, _ = provider._read_cache("calendar")
        self.assertEqual(len(cached), 3)

    def test_incomplete_calendar_without_fallback_blocks_unsafe_analysis(self):
        with patch.object(self.client, "trade_cal", return_value=pd.DataFrame([
            {"cal_date": "20261013", "is_open": 1}, {"cal_date": "20261015", "is_open": 1}
        ])):
            with self.assertRaisesRegex(provider.DataProviderError, "完整交易日历不可用"):
                provider.fetch_market_data("20261013", "20261015", force=True)

    def test_legacy_calendar_predecessor_gap_is_rejected(self):
        frame = pd.DataFrame([
            {"trade_date": "2026-10-13", "is_open": 1, "pretrade_date": "20261012"},
            {"trade_date": "2026-10-15", "is_open": 1, "pretrade_date": "20261014"},
        ])
        with self.assertRaisesRegex(provider.DataProviderError, "链条存在缺口"):
            provider._calendar_sessions(frame, "2026-10-13", "2026-10-15", full=False)

    def test_torn_cache_pair_is_not_trusted(self):
        provider.fetch_market_data("20261013", "20261015", force=True)
        path = provider.CACHE_DIR / "510300.SH.csv"
        with path.open("a", encoding="utf-8") as handle:
            handle.write("unexpected partial replacement\n")
        frame, metadata = provider._read_cache("510300.SH")
        self.assertTrue(frame.empty)
        self.assertEqual(metadata, {})

    def test_missing_benchmark_is_explicitly_partial_but_core_data_still_available(self):
        with patch.object(self.client, "index_daily", side_effect=RuntimeError("index source unavailable")):
            result = provider.fetch_market_data("20261013", "20261015", force=True)
        self.assertFalse(result["observations"].empty)
        self.assertEqual(result["metadata"]["auxiliary"]["benchmark"]["mode"], "unavailable")
        self.assertEqual(result["metadata"]["refresh_health"], "partial")

    def test_conflicting_share_duplicates_are_not_silently_chosen(self):
        frame = pd.DataFrame([
            {"trade_date": "20261014", "ts_code": "510300.SH", "fd_share": 100},
            {"trade_date": "20261014", "ts_code": "510300.SH", "fd_share": 999},
        ])
        with self.assertRaisesRegex(provider.DataProviderError, "同日记录存在冲突"):
            provider._dated(frame)


if __name__ == "__main__":
    unittest.main()
