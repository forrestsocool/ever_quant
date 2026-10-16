"""Numerical, missing-data and trailing-window invariants for the ETF radar."""

import json
from datetime import date, timedelta
from statistics import mean, stdev

import pytest

from national_team_radar.engine import DEFAULT_UNIVERSE, analyze


def sessions(count):
    result = []
    day = date(2026, 1, 5)
    while len(result) < count:
        if day.weekday() < 5:
            result.append(day.isoformat())
        day += timedelta(days=1)
    return result


def observations_for_flows(flows, codes=("510300.SH",), nav=4.0):
    """The first row is the anchor; flows apply to following sessions."""
    calendar = sessions(len(flows) + 1)
    observations = []
    for code in codes:
        share = 1_000_000.0
        for i, day in enumerate(calendar):
            if i:
                share += flows[i - 1] * 10_000 / nav
            observations.append({
                "trade_date": day, "ts_code": code, "total_share": share,
                "nav": nav, "close": nav, "amount": 12_345, "pct_chg": 0.1,
                "source": "test",
            })
    return observations, calendar


def by_date(result, day, code="510300.SH"):
    return next(row for row in result["etf_series"][code] if row["date"] == day)


def test_estimated_flow_uses_previous_nav_and_declared_units():
    observations, calendar = observations_for_flows([10.0])
    observations[-1]["nav"] = 99.0
    result = analyze(observations, calendar, ["510300.SH"])
    latest = result["etfs"][0]
    assert latest["total_share_yi"] == 102.5
    assert latest["delta_share_yi"] == 2.5
    assert latest["share_change_pct"] == 2.5
    assert latest["flow_yi"] == 10.0
    assert latest["previous_nav"] == 4.0
    assert result["summary"]["flow_yi"] == 10.0
    assert result["method"]["units"]["input_total_share"] == "万份"


def test_zscore_has_exactly_previous_twenty_sessions():
    baseline = [float(i % 5 - 2) for i in range(20)]
    observations, calendar = observations_for_flows(baseline + [15.0])
    result = analyze(observations, calendar, ["510300.SH"], {"sync_min": 1})
    latest = result["etfs"][0]
    assert latest["baseline_days"] == 20
    assert latest["z_score"] == pytest.approx((15 - mean(baseline)) / stdev(baseline))
    assert result["summary"]["z_score"] == latest["z_score"]
    assert latest["status"] == "大额净申购异动"
    assert result["summary"]["status"] == "大额净申购异动"
    assert result["alerts"][0]["direction"] == "in"
    assert result["series"][-2]["z_score"] is None


def test_adding_future_flow_never_changes_historical_baseline_or_alert():
    flows = [-1, 0, 1, -1, 0, 1, 15]
    observations, calendar = observations_for_flows(flows)
    early = analyze(observations, calendar, ["510300.SH"], {"window": 3, "sync_min": 1})
    extended_obs, extended_calendar = observations_for_flows(flows + [-80, 75])
    late = analyze(extended_obs, extended_calendar, ["510300.SH"], {"window": 3, "sync_min": 1})
    assert late["series"][:len(early["series"])] == early["series"]
    assert late["etf_series"]["510300.SH"][:len(early["series"])] == early["etf_series"]["510300.SH"]


@pytest.mark.parametrize("missing_field", ["total_share", "nav"])
def test_missing_previous_exchange_session_data_is_never_gap_differenced(missing_field):
    observations, calendar = observations_for_flows([1, 2, 3])
    observations[2][missing_field] = None
    result = analyze(observations, calendar, ["510300.SH"])
    assert result["etfs"][0]["flow_yi"] is None
    expected = "missing_previous_share" if missing_field == "total_share" else "missing_previous_nav"
    assert expected in result["etfs"][0]["quality_codes"]
    assert result["summary"]["flow_yi"] is None


def test_completely_absent_previous_session_is_not_skipped():
    observations, calendar = observations_for_flows([1, 2, 3])
    del observations[2]
    result = analyze(observations, calendar, ["510300.SH"])
    assert result["etfs"][0]["flow_yi"] is None
    assert "missing_previous_session" in result["etfs"][0]["quality_codes"]
    missing_day = by_date(result, calendar[2])
    assert not missing_day["valid"]
    assert "missing_current_observation" in missing_day["quality_codes"]


def test_exchange_calendar_allows_weekends_and_holidays_without_inventing_missing_days():
    calendar = ["2026-09-30", "2026-10-09"]
    observations = [
        {"trade_date": calendar[0], "ts_code": "510300.SH", "total_share": 1_000_000, "nav": 4},
        {"trade_date": calendar[1], "ts_code": "510300.SH", "total_share": 1_025_000, "nav": 4},
    ]
    result = analyze(observations, calendar, ["510300.SH"])
    assert result["summary"]["flow_yi"] == 10.0


def test_zero_is_valid_flow_but_zero_variance_is_not_a_zscore():
    observations, calendar = observations_for_flows([0] * 21)
    result = analyze(observations, calendar, ["510300.SH"])
    latest = result["etfs"][0]
    assert latest["flow_yi"] == 0
    assert latest["valid"] is True
    assert latest["z_score"] is None
    assert latest["baseline_days"] == 20
    assert "zero_baseline_variance" in latest["quality_codes"]
    assert result["summary"]["flow_20d_yi"] == 0
    assert result["alerts"] == []


@pytest.mark.parametrize("ratio", [2, 3, 0.5, 1 / 3, 1.25, 1.5, 1.6, 0.7])
def test_split_like_reciprocal_share_and_nav_change_is_quarantined(ratio):
    observations, calendar = observations_for_flows([1, 0, -1, 0])
    previous = observations[-2]
    observations[-1]["total_share"] = previous["total_share"] * ratio
    observations[-1]["nav"] = previous["nav"] / ratio
    result = analyze(observations, calendar, ["510300.SH"], {"window": 3})
    latest = result["etfs"][0]
    assert latest["quarantined"] is True
    assert latest["raw_flow_yi"] != 0
    assert latest["flow_yi"] is None
    assert "split_like_share_nav_jump" in latest["quality_codes"]
    assert result["summary"]["flow_yi"] is None


def test_genuine_large_share_creation_remains_an_eligible_signal():
    observations, calendar = observations_for_flows([-1, 0, 1, 0])
    previous = observations[-2]
    observations[-1]["total_share"] = previous["total_share"] * 1.25
    result = analyze(observations, calendar, ["510300.SH"], {"window": 3, "sync_min": 1})
    latest = result["etfs"][0]
    assert latest["quarantined"] is False
    assert latest["flow_yi"] > 10
    assert "large_share_jump" in latest["quality_codes"]
    assert latest["status"] == "大额净申购异动"


def test_missing_baseline_sessions_are_not_replaced_with_older_values():
    observations, calendar = observations_for_flows([-1, 0, 1] * 12 + [15])
    for observation in observations[22:35]:
        observation["total_share"] = None
    result = analyze(observations, calendar, ["510300.SH"], {"window": 20, "sync_min": 1})
    assert result["summary"]["flow_yi"] == 15
    assert result["summary"]["z_score"] is None
    assert result["summary"]["baseline_days"] < 20
    assert result["etfs"][0]["z_score"] is None
    assert result["alerts"] == []


def test_missing_baseline_recovers_after_full_adjacent_window():
    observations, calendar = observations_for_flows([-1, 0, 1, 0, -1, 0, 1, 15])
    observations[3]["total_share"] = None
    result = analyze(observations, calendar, ["510300.SH"], {"window": 3, "sync_min": 1})
    assert by_date(result, calendar[-2])["z_score"] is None
    assert result["summary"]["z_score"] == 15
    assert result["etfs"][0]["baseline_days"] == 3


def test_large_share_jump_without_current_nav_is_pending_not_a_flow():
    observations, calendar = observations_for_flows([-1, 0, 1, 0, 0])
    observations[-2]["total_share"] = observations[-3]["total_share"] * 2
    observations[-2]["nav"] = None
    observations[-1]["total_share"] = observations[-2]["total_share"] + 75_000
    observations[-1]["nav"] = 2
    result = analyze(observations, calendar, ["510300.SH"], {"window": 3, "sync_min": 1})
    jump = by_date(result, calendar[-2])
    assert jump["raw_flow_yi"] == 400
    assert jump["flow_yi"] is None
    assert jump["quarantined"] is True
    assert "share_jump_pending_nav" in jump["quality_codes"]
    assert result["summary"]["baseline_days"] == 2
    assert result["alerts"] == []


def test_confirmed_split_does_not_corrupt_next_session_share_difference():
    observations, calendar = observations_for_flows([-1, 0, 1, 0, 0])
    observations[-2]["total_share"] = observations[-3]["total_share"] * 1.5
    observations[-2]["nav"] = 4 / 1.5
    observations[-1]["total_share"] = observations[-2]["total_share"] + 15 * 10_000 / (4 / 1.5)
    observations[-1]["nav"] = 4 / 1.5
    result = analyze(observations, calendar, ["510300.SH"], {"window": 3})
    assert by_date(result, calendar[-2])["flow_yi"] is None
    assert result["summary"]["flow_yi"] == pytest.approx(15)
    assert result["summary"]["z_score"] is None
    assert result["summary"]["baseline_days"] == 2


def test_synchronized_redemptions_are_symmetric_and_identity_is_unconfirmed():
    codes = [item["ts_code"] for item in DEFAULT_UNIVERSE]
    observations, calendar = observations_for_flows([-1, 0, 1, -15], codes)
    result = analyze(observations, calendar, DEFAULT_UNIVERSE, {"window": 3})
    assert result["summary"]["flow_yi"] == -105
    assert result["summary"]["sync_out"] == 7
    assert result["summary"]["sync_in"] == 0
    assert result["summary"]["status"] == "大额净赎回异动"
    assert result["summary"]["identity_confirmed"] is False
    assert result["alerts"][0]["direction"] == "out"
    assert result["alerts"][0]["sync_count"] == 7


def test_partial_latest_share_date_has_no_variable_universe_sum():
    codes = [item["ts_code"] for item in DEFAULT_UNIVERSE]
    observations, calendar = observations_for_flows([1] * 22, codes)
    observations[-1]["total_share"] = None
    result = analyze(observations, calendar, DEFAULT_UNIVERSE)
    assert result["as_of"] == calendar[-1]
    assert result["summary"]["valid_count"] == 6
    assert result["summary"]["total_count"] == 7
    assert result["summary"]["flow_yi"] is None
    assert result["summary"]["flow_5d_yi"] is None
    assert result["summary"]["flow_20d_yi"] is None
    assert result["series"][-2]["flow_yi"] == 7
    assert result["last_complete"]["date"] == calendar[-2]
    assert result["last_complete"]["flow_yi"] == 7
    assert result["quality"]["coverage"]["missing_codes"] == [codes[-1]]


def test_quote_only_latest_day_does_not_hide_latest_disclosed_share_signal():
    codes = ["510300.SH", "510310.SH"]
    observations, calendar = observations_for_flows([-1, 0, 1, 15, 0], codes)
    for observation in observations:
        if observation["trade_date"] == calendar[-1]:
            observation["total_share"] = None
    result = analyze(observations, calendar, codes, {"window": 3, "sync_min": 2})
    assert result["as_of"] == calendar[-2]
    assert result["summary"]["flow_yi"] == 30
    assert result["summary"]["status"] == "大额净申购异动"
    assert result["quality"]["latest_quote_date"] == calendar[-1]
    assert result["quality"]["calendar_latest_session"] == calendar[-1]
    assert result["quality"]["pending_dates"] == [calendar[-1]]


def test_stale_share_and_quote_are_visible_even_if_both_stop_together():
    observations, calendar = observations_for_flows([1, 2, 3])
    result = analyze(observations[:-1], calendar, ["510300.SH"])
    assert result["as_of"] == calendar[-2]
    assert result["quality"]["pending_dates"] == [calendar[-1]]
    assert result["quality"]["quote_pending_dates"] == [calendar[-1]]
    assert any("份额待披露或数据待更新" in warning for warning in result["quality"]["warnings"])


def test_one_large_etf_does_not_satisfy_portfolio_synchrony():
    codes = ["510300.SH", "510310.SH", "510330.SH"]
    observations, calendar = observations_for_flows([-1, 0, 1, 0], codes)
    for observation in observations:
        if observation["trade_date"] == calendar[-1] and observation["ts_code"] == codes[0]:
            observation["total_share"] += 20 * 10_000 / 4
    result = analyze(observations, calendar, codes, {"window": 3})
    assert result["summary"]["sync_in"] == 1
    assert result["summary"]["flow_yi"] == 20
    assert result["summary"]["status"] == "观察"
    assert result["alerts"] == []


def test_synchrony_offset_by_redemption_is_a_separate_observation():
    codes = [item["ts_code"] for item in DEFAULT_UNIVERSE[:4]]
    observations, calendar = observations_for_flows([-1, 0, 1, 15], codes)
    observations[-1]["total_share"] = observations[-2]["total_share"] - 50 * 10_000 / 4
    result = analyze(observations, calendar, codes, {"window": 3})
    assert result["summary"]["flow_yi"] == -5
    assert result["summary"]["sync_in"] == 3
    assert result["alerts"] == []
    event = result["observation_events"][0]
    assert event["kind"] == "synchrony_observation"
    assert event["direction"] == "in"
    assert event["flow_yi"] == -5
    assert "观察" in event["title"]
    assert result["summary"]["status"] == "观察"


def test_a_below_baseline_positive_flow_is_never_called_redemption():
    observations, calendar = observations_for_flows([20, 21, 22, 10])
    result = analyze(observations, calendar, ["510300.SH"], {"window": 3, "sync_min": 1})
    assert result["etfs"][0]["z_score"] < -2
    assert result["etfs"][0]["status"] == "观察"
    assert result["alerts"] == []


def test_manual_evidence_is_not_scored_and_future_evidence_never_leaks():
    observations, calendar = observations_for_flows([1, 2, 3])
    result = analyze(observations, calendar, ["510300.SH"], evidence=[
        {"date": calendar[-1], "title": "官方公告线索", "url": "https://example.org/notice", "kind": "official"},
        {"date": "2099-01-01", "title": "未来公告", "url": "https://example.org/future", "kind": "official"},
        {"date": calendar[-1], "title": "坏链接", "url": "javascript:alert(1)", "kind": "market"},
    ])
    assert len(result["evidence"]) == 1
    assert result["evidence"][0]["kind"] == "official"
    assert result["evidence"][0]["source"] == "manual"
    assert result["evidence"][0]["affects_score"] is False
    assert result["summary"]["identity_confirmed"] is False
    assert len(result["quality"]["warnings"]) == 2


def test_conflicting_same_day_records_are_never_arbitrarily_selected():
    observations, calendar = observations_for_flows([1, 2])
    observations.append({**observations[-1], "total_share": observations[-1]["total_share"] + 10})
    result = analyze(observations, calendar, ["510300.SH"])
    assert result["etfs"][0]["flow_yi"] is None
    assert result["etfs"][0]["quarantined"] is True
    assert result["quality"]["conflicting_duplicates"] == 1


def test_nan_and_empty_inputs_are_json_safe():
    result = analyze([], sessions(3), ["510300.SH"])
    assert result["as_of"] is None
    assert result["summary"]["flow_yi"] is None
    assert result["etfs"][0]["date"] is None
    json.dumps(result, allow_nan=False)
    observations, calendar = observations_for_flows([0, 1])
    observations[-1]["close"] = float("nan")
    observations[-2]["nav"] = float("nan")
    result = analyze(observations, calendar, ["510300.SH"])
    assert result["etfs"][0]["close"] is None
    assert result["etfs"][0]["flow_yi"] is None
    json.dumps(result, allow_nan=False)


def test_five_day_total_requires_five_adjacent_complete_sessions():
    observations, calendar = observations_for_flows([1, 2, 3, 4, 5])
    complete = analyze(observations, calendar, ["510300.SH"])
    assert complete["summary"]["flow_5d_yi"] == 15
    observations[3]["total_share"] = None
    incomplete = analyze(observations, calendar, ["510300.SH"])
    assert incomplete["summary"]["flow_5d_yi"] is None


@pytest.mark.parametrize("config", [{"window": 1}, {"window": 2.5}, {"min_flow_yi": 0}, {"z_threshold": float("nan")}])
def test_invalid_configuration_is_rejected(config):
    with pytest.raises(ValueError):
        analyze([], [], ["510300.SH"], config)
