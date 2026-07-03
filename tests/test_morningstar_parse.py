"""Browserless unit tests for the Morningstar scraper's LIST-side PURE CORE.

These exercise only the deterministic functions in
scraper/morningstar_fund_details.py (the single canonical scraper) that turn
raw scraped rows into JSON and drive pagination — no selenium, no network.
selenium is imported lazily inside the module, so importing it here never
touches a browser.

Run: python -m pytest tests/test_morningstar_parse.py -v
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scraper"))

import morningstar_fund_details as ms  # noqa: E402


# ---------------------------------------------------------------------------
# rows_to_json
# ---------------------------------------------------------------------------
def test_rows_to_json_basic_mapping():
    headers = ["Fund Name", "Category", "1Y Return"]
    rows = [["Axis Bluechip", "Large Cap", "12.3"],
            ["SBI Small Cap", "Small Cap", "24.1"]]
    out = ms.rows_to_json(headers, rows)
    assert set(out) == {"Axis Bluechip", "SBI Small Cap"}
    assert out["Axis Bluechip"] == {"Category": "Large Cap", "1Y Return": "12.3"}
    # fund-name column is the key, not an attribute
    assert "Fund Name" not in out["Axis Bluechip"]


def test_rows_to_json_pads_short_rows_and_strips():
    headers = ["Fund Name", "Category", "Rating"]
    rows = [["  HDFC Flexi Cap  ", " Flexi Cap "]]      # missing Rating cell
    out = ms.rows_to_json(headers, rows)
    assert "HDFC Flexi Cap" in out                       # trimmed key
    assert out["HDFC Flexi Cap"] == {"Category": "Flexi Cap", "Rating": ""}


def test_rows_to_json_drops_spacer_rows():
    headers = ["Fund Name", "Category"]
    rows = [["", "junk"], ["Real Fund", "Value"]]
    out = ms.rows_to_json(headers, rows)
    assert list(out) == ["Real Fund"]


def test_rows_to_json_drops_empty_state_placeholder():
    headers = ["Fund Name", "Category"]
    rows = [["No records found. Please try again with some different search criteria.", ""],
            ["Real Fund", "Value"]]
    out = ms.rows_to_json(headers, rows)
    assert list(out) == ["Real Fund"]        # placeholder is not a fund


def test_rows_to_json_dedupes_duplicate_names():
    headers = ["Fund Name", "Category"]
    rows = [["Same Fund", "A"], ["Same Fund", "B"], ["Same Fund", "C"]]
    out = ms.rows_to_json(headers, rows)
    assert set(out) == {"Same Fund", "Same Fund (2)", "Same Fund (3)"}
    assert out["Same Fund"]["Category"] == "A"
    assert out["Same Fund (2)"]["Category"] == "B"


# ---------------------------------------------------------------------------
# is_next_disabled — the pagination stop condition
# ---------------------------------------------------------------------------
def test_next_enabled_when_plain_link():
    assert ms.is_next_disabled({"disabled": None, "class": ""}) is False


def test_next_disabled_via_disabled_attr():
    # <a disabled="disabled">Next &gt;</a>
    assert ms.is_next_disabled({"disabled": "disabled", "class": ""}) is True
    assert ms.is_next_disabled({"disabled": "true", "class": ""}) is True


def test_next_disabled_via_class():
    assert ms.is_next_disabled({"disabled": None, "class": "pager disabled"}) is True


def test_next_disabled_when_link_absent():
    assert ms.is_next_disabled(None, next_link_present=False) is True
    assert ms.is_next_disabled({"disabled": None, "class": ""},
                               next_link_present=False) is True


# ---------------------------------------------------------------------------
# normalize_scheme_name
# ---------------------------------------------------------------------------
def test_normalize_strips_plan_and_option_noise():
    a = ms.normalize_scheme_name("Axis Bluechip Fund - Direct Plan - Growth")
    b = ms.normalize_scheme_name("Axis Bluechip Fund [Regular] IDCW Payout")
    assert a == b == "axis bluechip fund"


# ---------------------------------------------------------------------------
# attach_to_catalog — exact-on-normalised-name join, deterministic
# ---------------------------------------------------------------------------
def test_attach_to_catalog_matches_and_reports():
    catalog = [
        {"scheme_code": "1", "scheme_name": "Axis Bluechip Fund - Direct - Growth"},
        {"scheme_code": "2", "scheme_name": "Nippon Small Cap Fund - Direct"},
    ]
    ms_data = {
        "Axis Bluechip Fund Regular Growth": {"Rating": "5"},
        "Some Fund Not In Catalog": {"Rating": "3"},
    }
    enriched, report = ms.attach_to_catalog(catalog, ms_data)
    assert enriched[0]["morningstar"]["Rating"] == "5"
    assert "morningstar" not in enriched[1]           # no fuzzy match
    assert report["matched"] == 1
    assert report["unmatched_catalog_names"] == ["Nippon Small Cap Fund - Direct"]
    assert report["unmatched_morningstar_names"] == ["Some Fund Not In Catalog"]


# ---------------------------------------------------------------------------
# merge_house_into — the concurrency-safe combine used by parallel workers
# ---------------------------------------------------------------------------
def test_merge_house_into_accumulates_without_loss():
    combined = {}
    a = ms.merge_house_into(combined, {"Fund A": {"x": "1"}})
    b = ms.merge_house_into(combined, {"Fund B": {"x": "2"}})
    assert a == 1 and b == 1
    assert combined == {"Fund A": {"x": "1"}, "Fund B": {"x": "2"}}


def test_merge_house_into_idempotent_on_identical():
    combined = {"Fund A": {"x": "1"}}
    added = ms.merge_house_into(combined, {"Fund A": {"x": "1"}})
    assert added == 0                                    # re-run / retry safe
    assert combined == {"Fund A": {"x": "1"}}


def test_merge_house_into_keeps_both_on_conflict():
    combined = {"Fund A": {"x": "1"}}
    added = ms.merge_house_into(combined, {"Fund A": {"x": "999"}})
    assert added == 1
    assert combined == {"Fund A": {"x": "1"}, "Fund A (2)": {"x": "999"}}


def test_merge_house_into_concurrent_threads_lose_nothing():
    import threading
    combined, lock = {}, threading.Lock()

    def worker(offset):
        for i in range(100):
            with lock:                                   # mirrors _merge_house
                ms.merge_house_into(combined, {f"Fund {offset}-{i}": {"n": str(i)}})

    threads = [threading.Thread(target=worker, args=(w,)) for w in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(combined) == 800                          # 8 workers * 100, none lost


# ---------------------------------------------------------------------------
# atomic_write_json — file is always valid JSON after write
# ---------------------------------------------------------------------------
def test_atomic_write_json_roundtrip(tmp_path):
    import json as _json
    p = str(tmp_path / "out.json")
    ms.atomic_write_json(p, {"b": 2, "a": 1})
    assert _json.load(open(p)) == {"a": 1, "b": 2}
    assert not os.path.exists(p + ".tmp")                # no leftover temp files
    # overwriting yields the new valid content, not a merge/append
    ms.atomic_write_json(p, {"c": 3})
    assert _json.load(open(p)) == {"c": 3}


# ---------------------------------------------------------------------------
# payload_hash — stable regardless of key insertion order
# ---------------------------------------------------------------------------
def test_payload_hash_order_independent():
    h1 = ms.payload_hash({"a": 1, "b": 2})
    h2 = ms.payload_hash({"b": 2, "a": 1})
    assert h1 == h2
    assert h1 != ms.payload_hash({"a": 1, "b": 3})
