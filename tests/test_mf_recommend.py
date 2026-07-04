"""Pytest wrapper + unit tests for the Morningstar-snapshot recommendation
engine (selection/mf_recommend.py, v1.1). Fully browserless and RNG-free.

The selftest builds a synthetic enriched snapshot and asserts:
  1. two runs produce identical run_hash (determinism)
  2. the weak fund fails the alpha-vs-category gate
  3. the hot-beta fund fails the high-beta-needs-alpha compensation gate
  4. the 65%-overlap twin never co-exists with the core pick
  5. Regular-plan variants never enter the universe
  6. every recommendation reason cites alpha, capture, recovery and quality
  7. changing the config changes config_hash
  8. a short investment horizon flips the leading risk tables to 3Y

Run: python -m pytest tests/test_mf_recommend.py -v
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "selection"))

import mf_recommend  # noqa: E402


def test_selftest_passes():
    assert mf_recommend.selftest() == 0


def test_num_handles_morningstar_cells():
    assert mf_recommend.num("11.13") == 11.13
    assert mf_recommend.num("-12.20") == -12.2
    assert mf_recommend.num("1,234.5") == 1234.5
    assert mf_recommend.num("—") is None
    assert mf_recommend.num("–") is None
    assert mf_recommend.num("") is None
    assert mf_recommend.num(None) is None


def test_parse_duration_months():
    p = mf_recommend.parse_duration_months
    assert p("5 Months") == 5.0
    assert p("1 Year 2 Months") == 14.0
    assert p("2 Years") == 24.0
    assert p("—") is None
    assert p(None) is None


def test_derive_horizon_preference():
    d = mf_recommend.derive_horizon_preference
    assert d(10) == ["10Y", "5Y", "3Y"]
    assert d(15) == ["10Y", "5Y", "3Y"]
    assert d(7) == ["5Y", "3Y"]
    assert d(3) == ["3Y", "5Y"]


def test_name_plan_filter():
    u = mf_recommend.DEFAULT_CONFIG["universe"]
    ok = mf_recommend.name_passes_plan_filter
    assert ok("Axis Bluechip Fund Direct Growth", u)
    assert not ok("Axis Bluechip Fund Regular Growth", u)
    assert not ok("Axis Bluechip Fund Direct IDCW", u)
    assert not ok("Axis X Fund Direct Payout Inc Dist cum Cap Wdrl", u)


def test_bucket_mapping():
    b = mf_recommend.DEFAULT_CONFIG["universe"]["buckets"]
    assert mf_recommend.bucket_for("Flexi Cap", b) == "core"
    assert mf_recommend.bucket_for("Mid-Cap", b) == "growth"
    assert mf_recommend.bucket_for("Small-Cap", b) == "aggressive"
    assert mf_recommend.bucket_for("Value", b) == "diversifier"
    assert mf_recommend.bucket_for("Liquid", b) is None    # not in any bucket


def test_horizon_metrics_math():
    fund = mf_recommend._synthetic_fund(
        "Flexi Cap", 2.5, 1.0, 0.9, 0.6, 0.90, 105, 90, 100, -10.0, -13.0,
        {"Stock A": 50.0, "Stock B": 50.0})
    m = mf_recommend.horizon_metrics(fund, "5Y")
    assert m["alpha_excess"] == 1.5              # 2.5 - 1.0
    assert m["sharpe_excess"] == 0.3             # 0.9 - 0.6
    assert m["capture_spread"] == 15.0           # 105 - 90
    assert m["drawdown_edge"] == 3.0             # -10 - (-13)
    assert m["std_edge"] == 1.0                  # 13 - 12
    assert m["drawdown_duration_months"] == 5.0  # "5 Months"


def test_cross_horizon_stability():
    fund = mf_recommend._synthetic_fund(
        "Flexi Cap", 2.5, 1.0, 0.9, 0.6, 0.90, 105, 90, 100, -10.0, -13.0,
        {"Stock A": 100.0})
    worst, n_pos, n_alpha, n_capture = mf_recommend.cross_horizon_stability(fund)
    assert worst == 1.5                          # same tables in all 3 horizons
    assert (n_pos, n_alpha, n_capture) == (3, 3, 3)


def test_equity_weights_skips_dash_and_dedupes():
    fund = {"detailed_portfolio": {"holdings": {"Equity": [
        {"Holdings": "A", "% Portfolio Weight": "5.18"},
        {"Holdings": "B", "% Portfolio Weight": "—"},       # exited position
        {"Holdings": "A", "% Portfolio Weight": "9.99"},    # dup name: first wins
    ]}}}
    assert mf_recommend.equity_weights(fund) == {"A": 5.18}


def test_portfolio_quality_weighted():
    fund = {"detailed_portfolio": {"holdings": {"Equity": [
        {"Holdings": "A", "% Portfolio Weight": "75", "Equity Star Rating": "4"},
        {"Holdings": "B", "% Portfolio Weight": "25", "Equity Star Rating": "2"},
        {"Holdings": "C", "% Portfolio Weight": "10", "Equity Star Rating": None},
    ]}}}
    # unrated C excluded; (4*75 + 2*25)/100 = 3.5
    assert mf_recommend.portfolio_quality(fund) == 3.5


def test_sector_effective_n():
    fund = {"detailed_portfolio": {"holdings": {"Equity": [
        {"Holdings": "A", "% Portfolio Weight": "50", "Sector": "Tech"},
        {"Holdings": "B", "% Portfolio Weight": "50", "Sector": "Energy"},
    ]}}}
    assert mf_recommend.sector_effective_n(fund) == 2.0   # perfectly split -> 2
    one = {"detailed_portfolio": {"holdings": {"Equity": [
        {"Holdings": "A", "% Portfolio Weight": "100", "Sector": "Tech"}]}}}
    assert mf_recommend.sector_effective_n(one) == 1.0


def test_reason_contains_driving_numbers():
    m = {"horizon_used": "5Y", "alpha": 2.5, "alpha_cat": 1.0, "alpha_excess": 1.5,
         "worst_alpha_excess": 0.8, "alpha_consistency": 3, "alpha_horizons": 3,
         "sharpe": 0.9, "sharpe_cat": 0.6, "sharpe_excess": 0.3, "beta": 0.9,
         "upside_capture": 105.0, "downside_capture": 90.0, "capture_spread": 15.0,
         "max_drawdown": -10.0, "max_drawdown_cat": -13.0, "drawdown_edge": 3.0,
         "drawdown_duration_months": 5.0, "portfolio_quality": 3.4,
         "top10_pct": 40.0, "turnover_pct": 45.0}
    reason = mf_recommend.build_reason(m, "core", "Flexi Cap")
    for token in ("+1.5", "worst-horizon excess +0.8", "3/3",
                  "risk-free hurdle", "105.0 of up-markets", "shallower",
                  "recovered from max drawdown in 5 months",
                  "avg star 3.4", "top-10 = 40% of assets", "turnover 45%"):
        assert token in reason, f"missing {token!r} in reason"


# ---------------------------------------------------------------------------
# append_metrics_history — dedup by enriched_at, never by wall-clock re-run
# ---------------------------------------------------------------------------
def _build_engine(tmp_path, enriched_at="2026-07-01T00:00:00+00:00"):
    fund = mf_recommend._synthetic_fund(
        "Flexi Cap", 2.5, 1.0, 0.9, 0.6, 0.90, 105, 90, 100, -10.0, -13.0,
        {"Stock A": 60.0, "Stock B": 40.0}, enriched_at=enriched_at)
    (tmp_path / "AMC_X.json").write_text(json.dumps(
        {"Solo Fund Direct Growth": fund}))
    cfg = json.loads(json.dumps(mf_recommend.DEFAULT_CONFIG))
    engine = mf_recommend.RecommendationEngine(str(tmp_path), cfg)
    engine.run()
    return engine


def test_append_metrics_history_first_write(tmp_path):
    engine = _build_engine(tmp_path)
    hpath = tmp_path / "metrics_history.jsonl"
    added = mf_recommend.append_metrics_history(engine, str(hpath))
    assert added == 1
    lines = hpath.read_text().strip().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["fund"] == "Solo Fund Direct Growth"
    assert row["enriched_at"] == "2026-07-01T00:00:00+00:00"
    assert "score" in row and "gates_passed" in row
    assert "alpha_excess" in row
    # raw holdings/risk blobs must NOT leak into history rows
    assert "holdings" not in row and "risk_ratings" not in row


def test_append_metrics_history_dedupes_unchanged_enriched_at(tmp_path):
    engine1 = _build_engine(tmp_path)
    hpath = tmp_path / "metrics_history.jsonl"
    mf_recommend.append_metrics_history(engine1, str(hpath))
    engine2 = _build_engine(tmp_path)          # same enriched_at, re-run
    added = mf_recommend.append_metrics_history(engine2, str(hpath))
    assert added == 0
    assert len(hpath.read_text().strip().splitlines()) == 1


def test_append_metrics_history_appends_on_new_enriched_at(tmp_path):
    engine1 = _build_engine(tmp_path, enriched_at="2026-07-01T00:00:00+00:00")
    hpath = tmp_path / "metrics_history.jsonl"
    mf_recommend.append_metrics_history(engine1, str(hpath))
    engine2 = _build_engine(tmp_path, enriched_at="2026-08-01T00:00:00+00:00")
    added = mf_recommend.append_metrics_history(engine2, str(hpath))
    assert added == 1
    lines = hpath.read_text().strip().splitlines()
    assert len(lines) == 2
    dates = {json.loads(l)["enriched_at"] for l in lines}
    assert dates == {"2026-07-01T00:00:00+00:00", "2026-08-01T00:00:00+00:00"}
