"""Network-free unit tests for the PURE CORE of nav_rolling_check.py —
mfapi payload parsing, strict Direct-Growth scheme matching, and the
rolling-return verdict logic (which reuses mf_select's tested math).

Run: python -m pytest tests/test_nav_rolling_check.py -v
"""

import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "selection"))

import nav_rolling_check as nrc  # noqa: E402


# ---------------------------------------------------------------------------
# parse_mfapi_history
# ---------------------------------------------------------------------------
def test_parse_mfapi_history_cleans_and_sorts_ascending():
    payload = {"data": [                       # mfapi lists newest FIRST
        {"date": "03-07-2026", "nav": "153.20"},
        {"date": "02-07-2026", "nav": "152.80"},
        {"date": "02-07-2026", "nav": "999.99"},   # dup date: first wins
        {"date": "01-07-2026", "nav": "0.00000"},  # zero NAV dropped
        {"date": "30-06-2026", "nav": "junk"},     # malformed dropped
        {"date": "29-06-2026", "nav": "151.00"},
        {"nav": "150.00"},                          # missing date dropped
    ]}
    series = nrc.parse_mfapi_history(payload)
    assert series == [(date(2026, 6, 29), 151.00),
                      (date(2026, 7, 2), 152.80),
                      (date(2026, 7, 3), 153.20)]
    assert nrc.parse_mfapi_history({}) == []
    assert nrc.parse_mfapi_history(None) == []


# ---------------------------------------------------------------------------
# match_scheme — must be strict: right fund, right plan, or refuse
# ---------------------------------------------------------------------------
CANDS = [
    {"schemeCode": 120503, "schemeName":
        "Axis Small Cap Fund - Direct Plan - Growth"},
    {"schemeCode": 120502, "schemeName":
        "Axis Small Cap Fund - Direct Plan - IDCW"},
    {"schemeCode": 120501, "schemeName":
        "Axis Small Cap Fund - Regular Plan - Growth"},
    {"schemeCode": 999999, "schemeName":
        "Axis Multicap Fund - Direct Plan - Growth"},
]


def test_match_scheme_picks_direct_growth_only():
    code, name, ambiguous = nrc.match_scheme(
        "Axis Small Cap Fund Direct Growth", CANDS)
    assert code == "120503" and "Direct Plan - Growth" in name
    assert ambiguous is None


def test_match_scheme_handles_morningstar_name_noise():
    # Morningstar-style name with stray hyphens/Option suffix still matches
    code, _, _ = nrc.match_scheme(
        "Axis Small Cap Fund -Direct Plan - Growth Option", CANDS)
    assert code == "120503"


def test_match_scheme_refuses_when_no_plan_match():
    # only Regular/IDCW variants available -> no match, never a guess
    code, name, ambiguous = nrc.match_scheme(
        "Axis Small Cap Fund Direct Growth", CANDS[1:3])
    assert code is None and name is None and ambiguous == []


def test_match_scheme_surfaces_ambiguity_instead_of_guessing():
    twins = [
        {"schemeCode": 1, "schemeName": "Acme Value Fund - Direct Plan - Growth"},
        {"schemeCode": 2, "schemeName": "Acme Value Fund Direct Growth"},
    ]
    code, _, ambiguous = nrc.match_scheme("Acme Value Fund Direct Growth", twins)
    assert code is None
    assert len(ambiguous) == 2 and any("1:" in a for a in ambiguous)


def test_match_scheme_requires_every_identity_token():
    # "Bluechip" fund must not match a "Midcap" scheme from the same AMC
    cands = [{"schemeCode": 7, "schemeName":
              "Acme Midcap Fund - Direct Plan - Growth"}]
    code, _, _ = nrc.match_scheme("Acme Bluechip Fund Direct Growth", cands)
    assert code is None


# ---------------------------------------------------------------------------
# finalists_from_report — picks + deduped bench, roles preserved
# ---------------------------------------------------------------------------
SAMPLE_REP = {
    "recommendations": [{"fund": "Core Pick"}, {"fund": "Growth Pick"}],
    "bench": [
        {"pick": "Core Pick", "bucket": "core",
         "alternates": [{"fund": "Core Sub"}, {"fund": "Shared Sub"}]},
        {"pick": "Growth Pick", "bucket": "growth",
         "alternates": [{"fund": "Shared Sub"},      # on two benches: deduped
                        {"fund": "Growth Pick"}]},   # already a pick: skipped
    ],
}


def test_finalists_from_report_includes_deduped_bench():
    got = nrc.finalists_from_report(SAMPLE_REP)
    assert got == [("Core Pick", "pick"), ("Growth Pick", "pick"),
                   ("Core Sub", "bench for Core Pick"),
                   ("Shared Sub", "bench for Core Pick")]


def test_finalists_from_report_no_bench_and_missing_bench_key():
    assert nrc.finalists_from_report(SAMPLE_REP, include_bench=False) == [
        ("Core Pick", "pick"), ("Growth Pick", "pick")]
    # reports generated before v1.4 have no `bench` key — must not crash
    assert nrc.finalists_from_report(
        {"recommendations": [{"fund": "Solo"}]}) == [("Solo", "pick")]


# ---------------------------------------------------------------------------
# evaluate_series — verdicts from synthetic deterministic NAV paths
# ---------------------------------------------------------------------------
def _series(days, nav_fn, start=date(2013, 1, 1), step=7):
    """Weekly synthetic series: nav_fn(i) -> value for point i."""
    return [(start + timedelta(days=i * step), nav_fn(i))
            for i in range(days // step)]


def test_evaluate_series_steady_grower_passes():
    # ~12 years of steady weekly growth with a deterministic zigzag so the
    # downside deviation is non-zero and Sortino is computable
    s = _series(12 * 365, lambda i: 100.0 * (1.0015 ** i) * (1.002 if i % 2 else 0.998))
    out = nrc.evaluate_series(s)
    assert out["verdict"] == "PASS"
    assert out["rolling3y"]["passed"] and out["rolling5y"]["passed"]
    assert out["rolling3y"]["pct_positive"] == 100.0
    assert out["rolling5y"]["worst_cagr"] > 0
    assert out["sortino_5y"] is not None and out["sortino_5y"] > 0
    assert out["history_years"] > 11


def test_evaluate_series_long_decline_fails():
    # 5 years up, then 4 years of steady decline, then 4 years up: several
    # 3Y windows sit fully inside the decline (negative CAGR) and the worst
    # 5Y window is negative -> both gates fail
    def nav(i):
        wk_up, wk_down = 5 * 52, 4 * 52
        if i < wk_up:
            return 100.0 * (1.002 ** i)
        if i < wk_up + wk_down:
            return 100.0 * (1.002 ** wk_up) * (0.9975 ** (i - wk_up))
        return (100.0 * (1.002 ** wk_up) * (0.9975 ** wk_down)
                * (1.002 ** (i - wk_up - wk_down)))
    out = nrc.evaluate_series(_series(13 * 365, nav))
    assert out["verdict"] == "FAIL"
    assert not out["rolling3y"]["passed"]
    assert out["rolling5y"]["worst_cagr"] < 0


def test_evaluate_series_young_fund_is_short_history_never_pass():
    # 3-year-old fund with DENSE daily NAVs -> genuinely young, not a data gap
    s = _series(3 * 365, lambda i: 100.0 * (1.0005 ** i), step=1)
    out = nrc.evaluate_series(s)
    assert out["verdict"] == "SHORT_HISTORY"
    assert out["rolling3y"] is None and out["rolling5y"] is None
    assert out["history_years"] < 7 and out["points_per_year"] > 200
    assert "young fund" in out["history_note"]


def test_evaluate_series_empty_is_incomplete():
    out = nrc.evaluate_series([])
    assert out["verdict"] == "INCOMPLETE_HISTORY"
    assert "no usable NAV" in out["history_note"]


def test_evaluate_series_sparse_download_is_incomplete_not_short():
    # 3 years but only monthly points (~12/yr) -> partial download, NOT a
    # genuinely young fund; must be flagged as a data problem
    s = _series(3 * 365, lambda i: 100.0 * (1.02 ** i), step=30)
    out = nrc.evaluate_series(s)
    assert out["verdict"] == "INCOMPLETE_HISTORY"
    assert out["points_per_year"] < nrc.MIN_NAV_POINTS_PER_YEAR
    assert "partial" in out["history_note"]


def test_evaluate_series_stale_feed_is_incomplete_even_when_long():
    # 12 years of dense data but the feed STOPPED years before as_of ->
    # merged/dead scheme or wrong scheme-code match; never evaluate as current
    from datetime import date
    s = _series(12 * 365, lambda i: 100.0 * (1.0005 ** i), step=1,
                start=date(2005, 1, 1))          # ends ~2017
    out = nrc.evaluate_series(s, as_of=date(2026, 7, 1))
    assert out["verdict"] == "INCOMPLETE_HISTORY"
    assert out["last_nav_age_days"] > nrc.STALE_NAV_MAX_DAYS
    assert "stale" in out["history_note"]
    # same series WITHOUT a stale as_of evaluates normally (fresh enough)
    assert nrc.evaluate_series(s, as_of=None)["verdict"] in ("PASS", "FAIL")


def test_evaluate_thresholds_come_from_dormant_framework():
    """DRIFT GUARD: the checker must apply mf_select.py's own gate values —
    a threshold fork between the two would silently weaken the second opinion."""
    import mf_select
    s = _series(12 * 365, lambda i: 100.0 * (1.0015 ** i))
    out = nrc.evaluate_series(s)
    assert (out["rolling3y"]["threshold_min"]
            == mf_select.DEFAULT_CONFIG["gates"]["rolling3y_pct_positive_min"])
    assert (out["rolling5y"]["threshold_min"]
            == mf_select.DEFAULT_CONFIG["gates"]["rolling5y_worst_min"])
