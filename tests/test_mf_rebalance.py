"""Unit tests for the PURE CORE of mf_rebalance.py (Stage 5) — NAV-derived
values, 5/25 drift, exact trade arithmetic, fresh-gate quality states and the
quality-outranks-drift decision precedence. No network, no I/O.

Run: python -m pytest tests/test_mf_rebalance.py -v
"""

import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "selection"))

import mf_rebalance as rb  # noqa: E402


SERIES = [(date(2026, 1, 1), 100.0), (date(2026, 1, 5), 102.0),
          (date(2026, 7, 1), 130.0)]


def test_nav_on_or_before():
    assert rb.nav_on_or_before(SERIES, date(2026, 1, 3)) == (date(2026, 1, 1), 100.0)
    assert rb.nav_on_or_before(SERIES, date(2026, 1, 5)) == (date(2026, 1, 5), 102.0)
    assert rb.nav_on_or_before(SERIES, date(2025, 12, 31)) is None
    assert rb.nav_on_or_before(SERIES, date(2027, 1, 1)) == (date(2026, 7, 1), 130.0)


def test_derive_current_value_shows_its_work():
    d = rb.derive_current_value(100_000, SERIES, date(2026, 1, 1))
    assert d["units"] == 1000.0 and d["nav_buy"] == 100.0
    assert d["current_value"] == 130_000        # 1000 units x 130
    assert d["nav_now_date"] == "2026-07-01"
    assert rb.derive_current_value(100_000, [], date(2026, 1, 1)) is None
    assert rb.derive_current_value(100_000, SERIES, date(2025, 1, 1)) is None


def test_compute_drift_dual_thresholds():
    targets = {"A": 40.0, "B": 30.0, "C": 30.0}
    # A grew hard: 55/105 = 52.38% (+12.38pp, +30.9% rel) -> breach on both
    current = {"A": 550_000, "B": 250_000, "C": 250_000}
    rows = {r["fund"]: r for r in rb.compute_drift(targets, current, 5, 25)}
    assert rows["A"]["breach"] and "BREACH" in rows["A"]["reason"]
    # B fell to 250/1050 = 23.81% (-6.19pp) — breaches the absolute rule too
    assert rows["B"]["breach"] and abs(rows["B"]["drift_pp"]) > 5
    # loosen the thresholds and the same numbers pass clean
    calm = {r["fund"]: r for r in rb.compute_drift(targets, current, 15, 40)}
    assert not any(r["breach"] for r in calm.values())


def test_compute_drift_relative_rule_catches_small_buckets():
    # a 5%-target fund at 8% is only +3pp (under 5pp) but +60% relative
    targets = {"A": 95.0, "B": 5.0}
    current = {"A": 920_000, "B": 80_000}
    rows = {r["fund"]: r for r in rb.compute_drift(targets, current, 5, 25)}
    assert rows["B"]["breach"] and rows["B"]["drift_rel_pct"] == 60.0
    # with only the absolute rule it would NOT breach
    rows2 = {r["fund"]: r for r in rb.compute_drift(targets, current, 5, 1e9)}
    assert rows2["B"]["breach"] is False


def test_rebalance_trades_net_to_zero_without_new_money():
    targets = {"A": 50.0, "B": 50.0}
    current = {"A": 700_000, "B": 300_000}
    t = rb.rebalance_trades(current, targets)
    assert t == {"A": -200_000, "B": 200_000}
    assert sum(t.values()) == 0


def test_rebalance_trades_new_money_can_be_pure_buy():
    targets = {"A": 50.0, "B": 50.0}
    current = {"A": 600_000, "B": 400_000}
    t = rb.rebalance_trades(current, targets, new_money=200_000)
    assert sum(t.values()) == 200_000
    assert all(v >= 0 for v in t.values())       # tax-friendly: no sells
    assert t == {"A": 0, "B": 200_000}


def test_quality_status_three_states():
    fresh = {"ranking": [{"fund": "Keeper", "rank": 3, "score": 71.2,
                          "gates": {"passed": True}}],
             "excluded_by_gates": [{"fund": "Slipper",
                                    "failed_checks": ["alpha_vs_category"]}],
             "recommendations": [], "bench": []}
    q = rb.quality_status(["Keeper", "Slipper", "Ghost"], fresh)
    assert q["Keeper"]["status"] == "PASS_GATES"
    assert "gate survivor" in q["Keeper"]["reason"]
    assert q["Slipper"]["status"] == "FAILS_GATES"
    assert q["Slipper"]["failed_checks"] == ["alpha_vs_category"]
    assert q["Ghost"]["status"] == "NOT_IN_SNAPSHOT"
    assert "INCONCLUSIVE" in q["Ghost"]["reason"]


def _drift_row(fund, breach, dpp=0.0):
    return {"fund": fund, "breach": breach, "drift_pp": dpp,
            "reason": "r", "target_pct": 25.0, "current_pct": 25.0 + dpp,
            "current_value": 100, "drift_rel_pct": 0.0}


def test_decide_quality_outranks_drift():
    drift = [_drift_row("A", breach=True, dpp=8.0),
             _drift_row("B", breach=False)]
    quality = {"A": {"status": "PASS_GATES", "reason": "ok"},
               "B": {"status": "FAILS_GATES", "reason": "bad",
                     "failed_checks": ["x"]}}
    actions, verdict = rb.decide(drift, quality, {})
    # B fails gates -> REPLACEMENT_REQUIRED even though only A drifted
    assert verdict == "REPLACEMENT_REQUIRED"
    assert actions["B"]["action"] == "REPLACE"
    assert actions["A"]["action"] == "TRIM"      # positive drift = trim


def test_decide_rolling_fail_forces_replace():
    drift = [_drift_row("A", breach=False)]
    quality = {"A": {"status": "PASS_GATES", "reason": "ok"}}
    actions, verdict = rb.decide(drift, quality, {"A": "FAIL"})
    assert verdict == "REPLACEMENT_REQUIRED"
    assert actions["A"]["action"] == "REPLACE"
    assert any("rolling check: FAIL" in r for r in actions["A"]["reasons"])


def test_decide_hold_add_and_inconclusive():
    drift = [_drift_row("A", breach=True, dpp=-7.0),
             _drift_row("B", breach=False), _drift_row("C", breach=False)]
    quality = {"A": {"status": "PASS_GATES", "reason": "ok"},
               "B": {"status": "PASS_GATES", "reason": "ok"},
               "C": {"status": "NOT_IN_SNAPSHOT", "reason": "inc"}}
    actions, verdict = rb.decide(drift, quality, {"A": "PASS", "B": "PASS"})
    assert verdict == "INCONCLUSIVE"             # missing data blocks trades
    assert actions["A"]["action"] == "ADD"       # negative drift = add
    assert actions["B"]["action"] == "HOLD"
    assert actions["C"]["action"] == "INCONCLUSIVE"
    # without C, the same portfolio is a plain rebalance
    actions2, verdict2 = rb.decide(drift[:2], quality, {})
    assert verdict2 == "REBALANCE_REQUIRED"
