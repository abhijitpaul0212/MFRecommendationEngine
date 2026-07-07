"""Unit tests for the PURE CORE of mf_allocate.py (Stage 4) — template
integrity, horizon banding, exact rounding, bucket redistribution and the
Stage 3 gating logic. No I/O, no prompts.

Run: python -m pytest tests/test_mf_allocate.py -v
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "selection"))

import mf_allocate as al  # noqa: E402


PICKS4 = [
    {"fund": "Core Fund", "bucket": "core", "fund_house": "A", "category": "Flexi Cap"},
    {"fund": "Growth Fund", "bucket": "growth", "fund_house": "B", "category": "Mid-Cap"},
    {"fund": "Aggr Fund", "bucket": "aggressive", "fund_house": "C", "category": "Small-Cap"},
    {"fund": "Div Fund", "bucket": "diversifier", "fund_house": "D", "category": "Value"},
]


def test_every_template_sums_to_100():
    assert set(al.ALLOCATION_TEMPLATES) == {
        (r, b) for r in al.RISK_PROFILES for b in al.HORIZON_BANDS}
    for key, w in al.ALLOCATION_TEMPLATES.items():
        assert sum(w.values()) == 100, f"{key} sums to {sum(w.values())}"
        assert set(w) == {"core", "growth", "aggressive", "diversifier"}
        assert all(v >= 0 for v in w.values())


def test_horizon_band_edges():
    assert al.horizon_band(4.9) is None          # under the 5y minimum
    assert al.horizon_band(5) == "5-10y"
    assert al.horizon_band(9.99) == "5-10y"
    assert al.horizon_band(10) == "10-15y"
    assert al.horizon_band(15) == "15y+"
    assert al.horizon_band(40) == "15y+"


def test_largest_remainder_sums_exactly():
    # 3 x 33.333...% of 100000 -> 33334 + 33333 + 33333 (name tie-break)
    pcts = [("a", 100 / 3), ("b", 100 / 3), ("c", 100 / 3)]
    amounts = al.largest_remainder_amounts(pcts, 100000)
    assert sum(amounts.values()) == 100000
    assert amounts == {"a": 33334, "b": 33333, "c": 33333}


def test_round_pcts_to_integers_practical_and_exact():
    # the real case that motivated this: 43.75 / 31.25 / 25.0 -> 44 / 31 / 25
    got = dict(al.round_pcts_to_integers(
        [("hdfc", 43.75), ("invesco", 31.25), ("axis", 25.0)]))
    assert got == {"hdfc": 44, "invesco": 31, "axis": 25}
    assert sum(got.values()) == 100
    # thirds: 34/33/33 with deterministic name tie-break; zero stays zero
    got = dict(al.round_pcts_to_integers(
        [("a", 100 / 3), ("b", 100 / 3), ("c", 100 / 3)]))
    assert got == {"a": 34, "b": 33, "c": 33} and sum(got.values()) == 100
    got = dict(al.round_pcts_to_integers([("x", 100.0), ("z", 0.0)]))
    assert got == {"x": 100, "z": 0}


def test_practical_amounts_round_figures_and_residue():
    pcts = [("a", 44), ("b", 31), ("c", 25)]
    # round total: every figure a clean multiple of 1000, sum exact
    amounts, note = al.practical_amounts(pcts, 1_000_000, 1000)
    assert amounts == {"a": 440_000, "b": 310_000, "c": 250_000}
    assert note is None
    # non-round total: all figures round except the largest, which absorbs
    # the sub-step residue so the WHOLE amount is still invested
    amounts, note = al.practical_amounts(pcts, 1_234_567, 1000)
    assert sum(amounts.values()) == 1_234_567
    assert amounts["b"] % 1000 == 0 and amounts["c"] % 1000 == 0
    assert amounts["a"] % 1000 == 567 and "residue" in note
    # step 1 degrades to the exact rupee-level split
    amounts, note = al.practical_amounts(pcts, 1_234_567, 1)
    assert sum(amounts.values()) == 1_234_567 and note is None


def test_full_portfolio_moderate_15y():
    plan = al.build_allocation(PICKS4, "moderate", 15, 1_000_000)
    by_fund = {r["fund"]: r for r in plan["rows"]}
    assert plan["band"] == "15y+"
    # template (moderate, 15y+): core 35 / growth 25 / aggressive 20 / div 20
    assert by_fund["Core Fund"]["pct"] == 35.0
    assert by_fund["Growth Fund"]["pct"] == 25.0
    assert by_fund["Aggr Fund"]["pct"] == 20.0
    assert by_fund["Div Fund"]["pct"] == 20.0
    assert sum(r["pct"] for r in plan["rows"]) == 100.0
    assert sum(r["amount_inr"] for r in plan["rows"]) == 1_000_000
    assert plan["warnings"] == []


def test_missing_bucket_redistributes_proportionally():
    picks = [p for p in PICKS4 if p["bucket"] != "aggressive"]
    plan = al.build_allocation(picks, "moderate", 15, 800_000)
    # aggressive's 20 redistributed over 35+25+20=80 -> scale 1.25 gives
    # 43.75/31.25/25 -> rounded to practical whole percents 44/31/25
    by_fund = {r["fund"]: r["pct"] for r in plan["rows"]}
    assert by_fund["Core Fund"] == 44
    assert by_fund["Growth Fund"] == 31
    assert by_fund["Div Fund"] == 25
    assert sum(by_fund.values()) == 100
    amounts = {r["fund"]: r["amount_inr"] for r in plan["rows"]}
    assert amounts == {"Core Fund": 352_000, "Growth Fund": 248_000,
                       "Div Fund": 200_000}      # round figures, exact sum
    assert any("unfilled bucket" in w for w in plan["warnings"])


def test_two_funds_same_bucket_split_practically():
    picks = PICKS4 + [{"fund": "Core Fund 2", "bucket": "core",
                       "fund_house": "E", "category": "Large-Cap"}]
    plan = al.build_allocation(picks, "moderate", 15, 500_000)
    by_fund = {r["fund"]: r["pct"] for r in plan["rows"]}
    # 35/2 = 17.5 each -> whole percents 18/17 (name tie-break), sum 100
    assert sorted([by_fund["Core Fund"], by_fund["Core Fund 2"]]) == [17, 18]
    assert sum(by_fund.values()) == 100
    assert sum(r["amount_inr"] for r in plan["rows"]) == 500_000
    assert all(r["amount_inr"] % 1000 == 0 for r in plan["rows"])


def test_zero_weight_bucket_warns_not_hides():
    # conservative 5-10y allocates 0 to aggressive: the fund shows at 0% with
    # a warning, never silently dropped
    plan = al.build_allocation(PICKS4, "conservative", 6, 100_000)
    by_fund = {r["fund"]: r["pct"] for r in plan["rows"]}
    assert by_fund["Aggr Fund"] == 0.0
    assert any("gets 0%" in w for w in plan["warnings"])
    assert sum(r["amount_inr"] for r in plan["rows"]) == 100_000


def test_concentration_warning_on_sparse_portfolio():
    # only the core pick survives: it takes 100% -> concentration warning
    plan = al.build_allocation(PICKS4[:1], "moderate", 15, 100_000)
    assert plan["rows"][0]["pct"] == 100.0
    assert any("concentration" in w for w in plan["warnings"])


def test_refuses_short_duration_and_bad_inputs():
    import pytest
    with pytest.raises(ValueError, match="unsuitable for short money"):
        al.build_allocation(PICKS4, "moderate", 3, 100_000)
    with pytest.raises(ValueError, match="risk must be one of"):
        al.build_allocation(PICKS4, "yolo", 15, 100_000)
    with pytest.raises(ValueError, match="no funds"):
        al.build_allocation([], "moderate", 15, 100_000)


def test_stage3_blockers_fail_blocks_unless_allowed():
    nav = [{"fund": "Core Fund", "verdict": "FAIL"},
           {"fund": "Growth Fund", "verdict": "PASS"},
           {"fund": "Aggr Fund", "verdict": "SHORT_HISTORY",
            "history_note": "fund has 4.9y..."},
           {"fund": "Div Fund", "verdict": "PASS"}]
    blockers, warnings = al.stage3_blockers(PICKS4, nav)
    assert len(blockers) == 1 and "Core Fund" in blockers[0]
    assert any("SHORT_HISTORY" in w for w in warnings)
    # --allow-failed downgrades the block to a warning
    blockers2, warnings2 = al.stage3_blockers(PICKS4, nav, allow_failed=True)
    assert blockers2 == []
    assert any("overridden" in w for w in warnings2)


def test_stage3_blockers_missing_run_or_verdict_warns():
    blockers, warnings = al.stage3_blockers(PICKS4, None)
    assert blockers == [] and any("has not been run" in w for w in warnings)
    blockers, warnings = al.stage3_blockers(
        PICKS4, [{"fund": "Core Fund", "verdict": "PASS"}])
    assert blockers == []
    assert sum("no Stage 3 verdict" in w for w in warnings) == 3


def test_build_follow_up_command_uses_failed_fund():
    args = type("Args", (), {
        "report": "recommendation_run/recommendations.json",
        "amount": 20000,
        "risk": "moderate",
        "years": 10,
    })()
    blockers = ["'Core Fund' FAILED the Stage 3 rolling-return check"]
    cmd = al.build_follow_up_command(args, blockers)
    assert "mf_recommend.py" in cmd
    assert "--exclude 'Core Fund'" in cmd
    assert "mf_allocate.py" in cmd


def test_render_markdown_plan_includes_stage3_warning_suggestion():
    plan = {"rows": [], "warnings": [
        "'Kotak Multicap Fund Direct Growth' has no Stage 3 verdict — "
        "re-run nav_rolling_check.py on this report"
    ]}
    md = al.render_markdown_plan(
        plan,
        amount_line="₹100,000 lumpsum",
        risk="moderate",
        years=10,
        band="10-15y",
        report_hash="abc123",
        plan_hash="def456",
        note="test note",
    )
    assert "### Warnings" in md
    assert "re-run nav_rolling_check.py on this report" in md
