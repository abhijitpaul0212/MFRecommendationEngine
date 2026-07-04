"""Browserless unit tests for the PURE CORE of morningstar_fund_details.py.

Covers URL derivation, star-rating/share-change/metric normalisation, and the
non-destructive nesting of enrichment data into existing fund attributes.
selenium is never imported at module import time, so this runs in CI.

Run: python -m pytest tests/test_fund_details_parse.py -v
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scraper"))

import morningstar_fund_details as fd  # noqa: E402


# ---------------------------------------------------------------------------
# derive_tab_urls
# ---------------------------------------------------------------------------
def test_derive_tab_urls_from_absolute_href():
    href = ("https://www.morningstar.in/mutualfunds/f000010uhv/"
            "axis-aggresive-hybrid-fund-direct-growth/snapshot.aspx")
    urls = fd.derive_tab_urls(href)
    stem = ("https://www.morningstar.in/mutualfunds/f000010uhv/"
            "axis-aggresive-hybrid-fund-direct-growth")
    assert urls["detailed_portfolio"] == f"{stem}/detailed-portfolio.aspx"
    assert urls["risk_ratings"] == f"{stem}/risk-ratings.aspx"


def test_derive_tab_urls_relative_href_and_fragment():
    urls = fd.derive_tab_urls("/mutualfunds/f0abc/some-fund/snapshot.aspx#")
    assert urls["detailed_portfolio"] == (
        "https://www.morningstar.in/mutualfunds/f0abc/some-fund/detailed-portfolio.aspx")


# ---------------------------------------------------------------------------
# parse_star_rating / signed_share_change / normalize_metric
# ---------------------------------------------------------------------------
def test_parse_star_rating():
    assert fd.parse_star_rating("Star rating : 3") == "3"
    assert fd.parse_star_rating("Star rating : 5") == "5"
    assert fd.parse_star_rating("") is None
    assert fd.parse_star_rating(None) is None


def test_signed_share_change_directions():
    assert fd.signed_share_change("2.00", "decrease") == "-2.00"
    assert fd.signed_share_change("2.68", "increase") == "2.68"
    assert fd.signed_share_change("0.00", "none") == "0.00"
    assert fd.signed_share_change("-1.50", "decrease") == "-1.50"  # no double sign
    assert fd.signed_share_change("—", "none") == "—"              # em-dash kept
    assert fd.signed_share_change("", "none") is None


def test_normalize_metric_r_squared():
    assert fd.normalize_metric("R2") == "R-Squared"
    assert fd.normalize_metric("R 2") == "R-Squared"
    assert fd.normalize_metric("Sharpe Ratio") == "Sharpe Ratio"
    assert fd.normalize_metric("  Standard   Deviation ") == "Standard Deviation"


# ---------------------------------------------------------------------------
# nest_fund_details — must never disturb existing list-level attributes
# ---------------------------------------------------------------------------
def test_nest_fund_details_preserves_existing_attrs():
    attrs = {"Action": "Factsheet", "Category": "Aggressive Allocation",
             "Latest NAV": "22.84", "NAV Date": "Jul 02, 2026"}
    portfolio = {"holdings_summary": {"Equity Holdings": "93"},
                 "holdings": {"Equity": [], "Bond": []}}
    risk = {"3Y": {"risk_volatility_measures": {}, "market_volatility_measures": {}}}
    out = fd.nest_fund_details(attrs, detail_url="http://x", detailed_portfolio=portfolio,
                               risk_ratings=risk, enriched_at="2026-07-04T00:00:00+00:00")
    for k, v in attrs.items():                     # nothing existing changed
        assert out[k] == v
    assert out["detail_url"] == "http://x"
    assert out["detailed_portfolio"] is portfolio
    assert out["risk_ratings"] is risk
    assert out["enriched_at"] == "2026-07-04T00:00:00+00:00"
    assert attrs == {"Action": "Factsheet", "Category": "Aggressive Allocation",
                     "Latest NAV": "22.84", "NAV Date": "Jul 02, 2026"}  # input untouched


def test_nest_fund_details_partial_is_noop_for_missing_parts():
    out = fd.nest_fund_details({"a": "1"})
    assert out == {"a": "1"}


def test_safe_house_name_matches_file_convention():
    import re
    house = "Axis Asset Management Company Limited"
    assert fd.safe_house_name(house) == re.sub(r"[^A-Za-z0-9]+", "_", house).strip("_")
    assert fd.safe_house_name("PPFAS Asset Management Pvt. Ltd") == "PPFAS_Asset_Management_Pvt_Ltd"


def test_is_direct_growth_plan_filter():
    ok = fd.is_direct_growth
    assert ok("Axis Bluechip Fund Direct Plan Growth")
    assert ok("Parag Parikh Flexi Cap Direct Growth")
    assert not ok("Axis Bluechip Fund Regular Growth")          # Regular
    assert not ok("Axis X Fund Direct IDCW")                    # IDCW
    assert not ok("Axis X Fund Direct Payout Inc Dist cum Cap Wdrl")
    assert not ok("Axis X Fund Direct Reinvestment Inc Dist cum Cap Wdrl")
    assert not ok("Axis X Fund Growth")                         # no Direct
    assert not ok(None)


def test_is_recently_enriched_uses_injected_now():
    from datetime import datetime, timezone
    now = datetime(2026, 8, 1, tzinfo=timezone.utc)
    fresh = fd.is_recently_enriched("2026-07-15T00:00:00+00:00", 30, now=now)
    stale = fd.is_recently_enriched("2026-06-01T00:00:00+00:00", 30, now=now)
    assert fresh is True
    assert stale is False


def test_is_recently_enriched_never_disabled_or_missing():
    assert fd.is_recently_enriched(None, 30) is False              # never enriched
    assert fd.is_recently_enriched("2026-07-15T00:00:00+00:00", 0) is False  # feature off
    assert fd.is_recently_enriched("2026-07-15T00:00:00+00:00", None) is False
    assert fd.is_recently_enriched("not-a-date", 30) is False       # malformed, never raises


def test_is_recently_enriched_handles_naive_timestamps():
    from datetime import datetime, timezone
    now = datetime(2026, 8, 1, tzinfo=timezone.utc)
    # a naive ISO string (no offset) must not crash the tz-aware comparison
    assert fd.is_recently_enriched("2026-07-15T00:00:00", 30, now=now) is True


def _complete_entry(equity_count=2):
    return {
        "risk_ratings": {"5Y": {"risk_volatility_measures": {
            "Alpha": {"Investment": "1.0", "Category": "0.5", "Index": "–"}}}},
        "detailed_portfolio": {
            "holdings_summary": {"Equity Holdings": str(equity_count),
                                 "Bond Holdings": "0"},
            "holdings": {"Equity": [{"Holdings": "A", "% Portfolio Weight": "60"},
                                    {"Holdings": "B", "% Portfolio Weight": "40"}][:equity_count],
                         "Bond": []}},
    }


def test_parse_count():
    assert fd.parse_count("93") == 93.0
    assert fd.parse_count("1,234") == 1234.0
    assert fd.parse_count("—") is None
    assert fd.parse_count("") is None
    assert fd.parse_count(None) is None


def test_enrichment_issues_complete_entry_is_clean():
    assert fd.enrichment_issues(_complete_entry()) == []


def test_enrichment_issues_never_enriched_is_none():
    assert fd.enrichment_issues({"Category": "Flexi Cap", "Latest NAV": "10"}) is None


def test_enrichment_issues_flags_empty_summary():
    e = _complete_entry()
    e["detailed_portfolio"]["holdings_summary"] = {}       # the Kotak Multicap case
    issues = fd.enrichment_issues(e)
    assert any("holdings_summary empty" in i for i in issues)


def test_enrichment_issues_flags_equity_error_and_empty_with_count():
    e = _complete_entry()
    e["detailed_portfolio"]["holdings"]["Equity"] = {"error": "boom"}
    assert any("Equity holdings error" in i for i in fd.enrichment_issues(e))
    e = _complete_entry()
    e["detailed_portfolio"]["holdings"]["Equity"] = []      # but summary says 2
    assert any("Equity holdings empty but summary says 2" in i
               for i in fd.enrichment_issues(e))


def test_enrichment_issues_zero_equity_and_bare_bond_are_ok():
    e = _complete_entry(equity_count=0)
    e["detailed_portfolio"]["holdings_summary"]["Equity Holdings"] = "0"
    assert fd.enrichment_issues(e) == []                    # debt/index fund: fine
    e = _complete_entry()
    e["detailed_portfolio"]["holdings_summary"]["Bond Holdings"] = "5"
    assert fd.enrichment_issues(e) == []   # empty Bond list tolerated (site hides tab)
    e["detailed_portfolio"]["holdings"]["Bond"] = {"error": "x"}
    assert any("Bond holdings error" in i for i in fd.enrichment_issues(e))


def test_enrichment_issues_flags_missing_or_empty_risk():
    e = _complete_entry()
    del e["risk_ratings"]
    assert "no risk_ratings" in fd.enrichment_issues(e)
    e = _complete_entry()
    e["risk_ratings"] = {"5Y": {"risk_volatility_measures": {}}}
    assert any("risk tables empty" in i for i in fd.enrichment_issues(e))


def test_allowed_workers_policy():
    aw = fd.allowed_workers
    assert aw(6, 4.5) == 6          # plenty of headroom: full fleet
    assert aw(6, 2.5) == 3          # 2-3 GB: half
    assert aw(6, 1.5) == 1          # 1-2 GB: single sequential worker
    assert aw(6, 0.7) == 0          # < 1 GB: full pause
    assert aw(1, 2.5) == 1          # half of 1 floors at 1
    assert aw(6, None) == 6         # unknown -> fail open (watchdog still alerts)


def test_parse_vm_stat():
    sample = (
        "Mach Virtual Memory Statistics: (page size of 16384 bytes)\n"
        "Pages free:                                    1000.\n"
        "Pages active:                                 85070.\n"
        "Pages inactive:                                2000.\n"
        "Pages speculative:                              500.\n")
    assert fd.parse_vm_stat(sample) == (1000 + 2000 + 500) * 16384
    assert fd.parse_vm_stat("") == 0


def test_available_memory_gb_returns_sane_value():
    v = fd.available_memory_gb()
    assert v is None or (0 < v < 2048)     # sane on any real machine


def test_slot_gating_acquire_release():
    s = fd.MorningstarScraper("/tmp/x", workers=2)
    assert s._acquire_slot() is True
    assert s._acquire_slot() is True       # both slots taken
    s._set_max_active(0)                   # governor pauses everything
    assert s._acquire_slot() is False      # no slot; caller should drop browser
    s._release_slot()
    s._release_slot()
    s._set_max_active(1)
    assert s._acquire_slot() is True       # resumes with reduced concurrency
    s._release_slot()


def test_merge_list_preserving_enrichment():
    existing = {
        "Fund A": {"Category": "Old Cat", "Latest NAV": "10",
                   "risk_ratings": {"3Y": {}}, "enriched_at": "2026-01-01"},
        "Fund Gone": {"Category": "X", "Latest NAV": "1"},
    }
    fresh = {
        "Fund A": {"Category": "New Cat", "Latest NAV": "11"},
        "Fund B": {"Category": "Y", "Latest NAV": "2"},
    }
    merged = fd.merge_list_preserving_enrichment(existing, fresh)
    assert merged["Fund A"]["Latest NAV"] == "11"            # list attrs refreshed
    assert merged["Fund A"]["Category"] == "New Cat"
    assert merged["Fund A"]["risk_ratings"] == {"3Y": {}}    # enrichment preserved
    assert merged["Fund A"]["enriched_at"] == "2026-01-01"
    assert "Fund Gone" not in merged                          # delisted funds dropped
    assert merged["Fund B"] == {"Category": "Y", "Latest NAV": "2"}


def test_clean_collapses_whitespace():
    assert fd.clean("  %  Assets in\n  Top 10 Holdings ") == "% Assets in Top 10 Holdings"
    assert fd.clean(None) == ""
