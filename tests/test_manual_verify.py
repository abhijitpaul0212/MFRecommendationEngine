"""Stage 3.5 manual-verification gate — pure-logic tests (browserless)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "selection"))
import manual_verify as mv


# ---- tenure ---------------------------------------------------------------
def test_tenure_years_is_explicit_and_reproducible():
    assert mv.tenure_years("2013-05-13", "2026-07-07") == 13.15
    assert mv.tenure_years("2025-02-19", "2026-07-07") == 1.38
    assert mv.tenure_years(None, "2026-07-07") is None
    assert mv.tenure_years("2020-01-01", None) is None


# ---- sortino --------------------------------------------------------------
def test_sortino_pass_beats_both():
    status, _ = mv.assess_sortino(1.19, 0.61, 0.74)
    assert status == "PASS"


def test_sortino_flag_beats_benchmark_but_lags_category():
    # Axis Small Cap: 0.95 > 0.92 benchmark but < 1.03 category
    status, _ = mv.assess_sortino(0.95, 0.92, 1.03)
    assert status == "FLAG"


def test_sortino_fail_below_benchmark():
    status, _ = mv.assess_sortino(0.50, 0.71, 0.94)
    assert status == "FAIL"


def test_sortino_unknown_when_missing():
    assert mv.assess_sortino(None, 0.6, 0.7)[0] == "UNKNOWN"


# ---- tenure tiers ---------------------------------------------------------
def test_tenure_tiers():
    assert mv.assess_tenure(13.15)[0] == "STRONG"
    assert mv.assess_tenure(3.5)[0] == "OK"
    assert mv.assess_tenure(2.93)[0] == "CAUTION"
    assert mv.assess_tenure(1.38)[0] == "FLAG"
    assert mv.assess_tenure(None)[0] == "UNKNOWN"


# ---- verdict combination --------------------------------------------------
def test_fund_verdict_matrix():
    assert mv.fund_verdict("PASS", "STRONG") == "VERIFIED"
    assert mv.fund_verdict("PASS", "CAUTION") == "REVIEW"     # short tenure only
    assert mv.fund_verdict("FLAG", "OK") == "REVIEW"          # below-cat sortino only
    assert mv.fund_verdict("FLAG", "FLAG") == "WEAK"          # both structural
    assert mv.fund_verdict("FAIL", "STRONG") == "REJECT"      # can't beat index
    assert mv.fund_verdict("UNKNOWN", "STRONG") == "REVIEW"   # unverified != clean


# ---- end-to-end assessment ------------------------------------------------
def _manual():
    return {
        "as_of": "2026-07-07",
        "run_hash": "abc123",
        "funds": [
            {"fund": "Parag Parikh Flexi Cap Direct Growth", "bucket": "core",
             "sortino": {"fund": 1.19, "benchmark": 0.61, "category": 0.74},
             "manager": {"name": "Rajeev Thakkar", "since": "2013-05-13"}},
            {"fund": "Quant Multi Cap Fund Growth Option Direct Plan",
             "bucket": "diversifier",
             "sortino": {"fund": 0.76, "benchmark": 0.71, "category": 0.94},
             "manager": {"name": "Ayusha Kumbhat", "since": "2025-02-19"}},
        ],
    }


def test_build_assessment_orders_worst_last_and_rolls_up():
    a = mv.build_assessment(_manual())
    # worst-verdict rollup is WEAK (Quant)
    assert a["portfolio_verdict"] == "WEAK"
    assert a["clean"] is False
    # rows sorted best->worst: Parag Parikh (VERIFIED) first, Quant (WEAK) last
    assert a["rows"][0]["verdict"] == "VERIFIED"
    assert a["rows"][-1]["verdict"] == "WEAK"


def test_manual_hash_is_deterministic():
    a1 = mv.build_assessment(_manual())
    a2 = mv.build_assessment(_manual())
    assert mv.manual_hash(a1) == mv.manual_hash(a2)


def test_all_clean_portfolio_is_verified():
    m = {"as_of": "2026-07-07", "funds": [
        {"fund": "X", "bucket": "core",
         "sortino": {"fund": 1.2, "benchmark": 0.6, "category": 0.7},
         "manager": {"name": "A", "since": "2015-01-01"}}]}
    a = mv.build_assessment(m)
    assert a["clean"] is True
    assert a["portfolio_verdict"] == "VERIFIED"
