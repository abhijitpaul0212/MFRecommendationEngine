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
