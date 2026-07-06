#!/usr/bin/env python3
"""
mf_allocate.py — STAGE 4: turn the verified finalists into an exact
investment breakdown (percent + amount per fund) from three manual inputs:
total amount, risk appetite, and investment duration. Supports both a
one-time LUMPSUM and a recurring monthly SIP (--frequency) — the bucket
weighting math is identical either way; only the amount's meaning and the
plan's recorded schedule differ, and Stage 5 reads that schedule back to
value a SIP portfolio correctly (see mf_rebalance.py).

Position in the process (README quick guide):
  Stage 2 recommendations.json -> Stage 3 nav_rolling_check.json -> THIS
  SCRIPT -> allocation_plan.json / allocation_plan.md -> place orders

Design contract (same ethos as the engines):
  - DETERMINISTIC: allocation weights come from an explicit 3x3 lookup table
    (risk profile x horizon band), not from arithmetic tilts — every number
    below is visible, auditable and unit-tested to sum to 100. Same inputs,
    same plan, always.
  - BUCKET-DRIVEN: the split allocates to the engine's buckets (core /
    growth / aggressive / diversifier), then equally among funds within a
    bucket. If a bucket has no pick (a valid engine outcome), its weight is
    redistributed proportionally across the buckets that ARE filled — and the
    plan says so.
  - HONEST GATES, HONEST WARNINGS: refuses to allocate for a duration under
    5 years (an all-equity portfolio is the wrong instrument — that is a
    statement about suitability, not a config default to override). Refuses
    to allocate to a pick that FAILED the Stage 3 NAV check unless
    --allow-failed (the bench/--exclude loop exists precisely for this).
    Warns — without silently "fixing" — on concentration (any single fund
    > 40%), on SHORT_HISTORY picks, and when Stage 3 was never run.
  - PRACTICAL, EXACT ARITHMETIC: percentages are WHOLE numbers (largest
    remainder to exactly 100 — 43.75% would be impossible to place as an
    order; 44% is not) and amounts are multiples of --step (default ₹1,000).
    The whole amount is still invested: a non-round total's sub-step residue
    goes to the largest allocation, noted in the plan. --step 1 restores
    rupee-exact splits.

Inputs (prompted interactively when flags are omitted):
  --amount 1000000          lumpsum total, OR the monthly SIP installment
                            amount when --frequency sip — in rupees
  --risk moderate           conservative | moderate | aggressive
  --years 15                intended holding duration in years
  --frequency lumpsum|sip   default lumpsum
  --sip-day 5, --start-date 2026-07-05   SIP only: the recurring debit day
                            (1-28, avoids short-month ambiguity) and the
                            first installment date. Recorded in the plan so
                            Stage 5 can reconstruct every installment.

Usage:
  python selection/mf_allocate.py --report recommendation_run/recommendations.json \
      --amount 1000000 --risk moderate --years 15                    # lumpsum
  python selection/mf_allocate.py --report recommendation_run/recommendations.json \
      --amount 25000 --risk moderate --years 15 \
      --frequency sip --sip-day 5 --start-date 2026-07-05             # SIP

Stdlib only. Not investment advice — arithmetic over your own inputs.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mf_select import r6, sha256_obj  # noqa: E402

RISK_PROFILES = ("conservative", "moderate", "aggressive")
HORIZON_BANDS = ("5-10y", "10-15y", "15y+")
FREQUENCIES = ("lumpsum", "sip")
MIN_YEARS = 5                  # below this an all-equity plan is refused
CONCENTRATION_WARN_PCT = 40.0  # any single fund above this draws a warning
MIN_SIP_DAY, MAX_SIP_DAY = 1, 28   # avoids short-month ambiguity (Feb 29-31)

# Explicit (risk x horizon) -> bucket weights, each row summing to 100.
# A LOOKUP TABLE, deliberately: no formulaic tilts to reverse-engineer. Logic
# encoded: longer runway earns more growth/aggressive; conservative profiles
# anchor on core + diversifier (the engine's diversifier bucket holds the
# allocation/hybrid categories — the calmest equity exposure it selects).
ALLOCATION_TEMPLATES = {
    ("conservative", "5-10y"):  {"core": 50, "growth": 10, "aggressive": 0,  "diversifier": 40},
    ("conservative", "10-15y"): {"core": 45, "growth": 15, "aggressive": 5,  "diversifier": 35},
    ("conservative", "15y+"):   {"core": 45, "growth": 20, "aggressive": 10, "diversifier": 25},
    ("moderate", "5-10y"):      {"core": 45, "growth": 20, "aggressive": 5,  "diversifier": 30},
    ("moderate", "10-15y"):     {"core": 40, "growth": 25, "aggressive": 15, "diversifier": 20},
    ("moderate", "15y+"):       {"core": 35, "growth": 25, "aggressive": 20, "diversifier": 20},
    ("aggressive", "5-10y"):    {"core": 40, "growth": 25, "aggressive": 15, "diversifier": 20},
    ("aggressive", "10-15y"):   {"core": 30, "growth": 30, "aggressive": 25, "diversifier": 15},
    ("aggressive", "15y+"):     {"core": 25, "growth": 30, "aggressive": 30, "diversifier": 15},
}


# ---------------------------------------------------------------------------
# Pure core (unit-tested, no I/O)
# ---------------------------------------------------------------------------

def horizon_band(years):
    """Duration -> template band. None below MIN_YEARS: an all-equity plan is
    the wrong instrument for short money and this tool won't produce one."""
    if years < MIN_YEARS:
        return None
    if years < 10:
        return "5-10y"
    if years < 15:
        return "10-15y"
    return "15y+"


def largest_remainder_amounts(pcts, total):
    """[(fund, pct)] + integer total -> {fund: integer amount} summing EXACTLY
    to total. Floor first, then hand out the leftover rupees by largest
    fractional part (fund-name tie-break — deterministic)."""
    raw = {f: total * p / 100.0 for f, p in pcts}
    base = {f: int(math.floor(v)) for f, v in raw.items()}
    leftover = total - sum(base.values())
    by_frac = sorted(raw, key=lambda f: (-(raw[f] - base[f]), f))
    for f in by_frac[:leftover]:
        base[f] += 1
    return base


def round_pcts_to_integers(pcts):
    """[(fund, float pct)] -> [(fund, int pct)] summing EXACTLY to 100, by
    largest remainder (fund-name tie-break). Fractional percentages are
    impractical to place as orders — 43.75% becomes 44%. Zero stays zero."""
    floors = {f: int(math.floor(p)) for f, p in pcts}
    leftover = 100 - sum(floors.values())
    by_frac = sorted(pcts, key=lambda fp: (-(fp[1] - floors[fp[0]]), fp[0]))
    bump = {f for f, _ in by_frac[:leftover]}
    return [(f, floors[f] + (1 if f in bump else 0)) for f, _ in pcts]


def practical_amounts(pcts, total, step):
    """Integer pcts + total -> {fund: amount} where every amount is a multiple
    of `step` (default ₹1,000 — figures you can actually type into an order),
    EXCEPT that when the total itself isn't a multiple of `step` the sub-step
    residue is added to the largest allocation so the WHOLE amount is still
    invested. Returns (amounts, residue_note_or_None). Deterministic."""
    if step <= 1:
        return largest_remainder_amounts(pcts, total), None
    residue = total % step
    chunks = largest_remainder_amounts(pcts, (total - residue) // step)
    amounts = {f: n * step for f, n in chunks.items()}
    note = None
    if residue:
        biggest = max(amounts, key=lambda f: (amounts[f], f))
        amounts[biggest] += residue
        note = (f"total ₹{total:,} is not a multiple of the ₹{step:,} step — "
                f"the ₹{residue:,} residue was added to '{biggest}' so the "
                f"whole amount is invested")
    return amounts, note


def build_allocation(picks, risk, years, amount, step=1000):
    """The whole plan, purely from inputs.

    picks: [{"fund", "bucket", "fund_house", "category"}] — the engine's
    recommendations (post Stage 3 gating, which the caller enforces).
    Percentages are PRACTICAL whole numbers (largest-remainder to exactly
    100) and amounts are multiples of `step` (default ₹1,000) — figures you
    can actually place as orders — while the whole amount is still invested.
    Returns {rows, weights_used, warnings, band} or raises ValueError on
    unsuitable inputs. Deterministic throughout."""
    if risk not in RISK_PROFILES:
        raise ValueError(f"risk must be one of {RISK_PROFILES}, got {risk!r}")
    band = horizon_band(years)
    if band is None:
        raise ValueError(
            f"duration {years}y is under the {MIN_YEARS}y minimum — an "
            f"all-equity portfolio is unsuitable for short money; park it in "
            f"debt/liquid instruments instead (outside this tool's scope)")
    if not picks:
        raise ValueError("no funds to allocate — run Stages 2 and 3 first")
    template = ALLOCATION_TEMPLATES[(risk, band)]
    warnings = []

    by_bucket = {}
    for p in picks:
        by_bucket.setdefault(p["bucket"], []).append(p)
    # weight only the buckets that actually have funds; scale so the missing
    # buckets' weight redistributes proportionally instead of vanishing
    present = {b: template[b] for b in by_bucket}
    missing = sorted(b for b in template if b not in by_bucket and template[b] > 0)
    scale_base = sum(present.values())
    if scale_base <= 0:
        raise ValueError(
            f"every pick sits in a bucket weighted 0 under ({risk}, {band}) — "
            f"pick a different risk profile or fix the portfolio first")
    if missing:
        warnings.append(
            f"unfilled bucket(s) {missing}: their "
            f"{r6(100 - scale_base)}% weight was redistributed "
            f"proportionally across the filled buckets")
    scale = 100.0 / scale_base

    pcts = []
    for b in sorted(by_bucket):
        bucket_pct = present[b] * scale
        funds = sorted(by_bucket[b], key=lambda p: p["fund"])
        if present[b] == 0:
            warnings.append(
                f"'{funds[0]['fund']}' ({b}) gets 0% under ({risk}, {band}) — "
                f"this profile allocates nothing to the {b} bucket; drop the "
                f"fund or reconsider the risk/duration inputs")
        for p in funds:                       # equal split within a bucket
            pcts.append((p["fund"], r6(bucket_pct / len(funds))))
    pcts = round_pcts_to_integers(pcts)       # 43.75% -> 44%: order-friendly
    for fund, pct in pcts:
        if pct > CONCENTRATION_WARN_PCT:
            warnings.append(
                f"'{fund}' takes {pct}% (> {CONCENTRATION_WARN_PCT}% "
                f"concentration line) — a consequence of unfilled bucket "
                f"slots; consider completing the portfolio before investing "
                f"the full amount")
    amounts, residue_note = practical_amounts(pcts, amount, step)
    if residue_note:
        warnings.append(residue_note)
    meta = {p["fund"]: p for p in picks}
    rows = [{"fund": f, "bucket": meta[f]["bucket"],
             "fund_house": meta[f].get("fund_house"),
             "category": meta[f].get("category"),
             "pct": pct, "amount_inr": amounts[f]}
            for f, pct in pcts]
    return {"rows": rows, "weights_used": dict(template), "band": band,
            "warnings": warnings}


def stage3_blockers(picks, nav_results, allow_failed=False):
    """Cross-check the picks against Stage 3 verdicts. Returns
    (blockers, warnings): blockers stop the allocation (FAIL picks, unless
    allow_failed); warnings ride along in the plan (SHORT_HISTORY etc.).
    nav_results: the `results` list from nav_rolling_check.json, or None if
    Stage 3 was never run (itself a warning). Pure."""
    warnings, blockers = [], []
    if nav_results is None:
        warnings.append(
            "Stage 3 (nav_rolling_check.py) has not been run on this report — "
            "the picks' rolling-return consistency is unverified")
        return blockers, warnings
    verdicts = {r["fund"]: r for r in nav_results}
    for p in picks:
        v = verdicts.get(p["fund"])
        if v is None:
            warnings.append(f"'{p['fund']}' has no Stage 3 verdict — re-run "
                            f"nav_rolling_check.py on this report")
            continue
        if v["verdict"] == "FAIL":
            msg = (f"'{p['fund']}' FAILED the Stage 3 rolling-return check — "
                   f"swap in a PASSing bench substitute or re-run the engine "
                   f"with --exclude before allocating")
            if allow_failed:
                warnings.append(msg + " (overridden by --allow-failed)")
            else:
                blockers.append(msg)
        elif v["verdict"] != "PASS":
            warnings.append(
                f"'{p['fund']}' is {v['verdict']} in Stage 3 "
                f"({(v.get('history_note') or v.get('note') or '')[:120]})")
    return blockers, warnings


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _prompt_missing(args):
    """Interactive fallback for the manual inputs — Stage 4 is the one stage
    designed to ask the human. Loops until each answer parses. --frequency
    defaults to lumpsum via argparse, so existing lumpsum callers see no new
    prompts; SIP's extra two questions only fire when --frequency sip is set."""
    label = ("Monthly SIP installment amount" if args.frequency == "sip"
             else "Total amount to invest")
    while args.amount is None:
        try:
            v = int(input(f"{label} (INR, e.g. 1000000): ")
                    .replace(",", "").strip())
            args.amount = v if v > 0 else None
        except (ValueError, EOFError):
            print("  enter a positive whole-rupee amount", flush=True)
    while args.risk is None:
        v = input(f"Risk appetite {RISK_PROFILES}: ").strip().lower()
        args.risk = v if v in RISK_PROFILES else None
        if args.risk is None:
            print(f"  choose one of {', '.join(RISK_PROFILES)}", flush=True)
    while args.years is None:
        try:
            args.years = float(input("Investment duration in years (e.g. 15): ")
                               .strip())
        except (ValueError, EOFError):
            print("  enter a number of years", flush=True)
    if args.frequency == "sip":
        while args.start_date is None:
            v = input("First SIP installment date (YYYY-MM-DD) "
                      "[today]: ").strip()
            if not v:
                args.start_date = datetime.now(timezone.utc).date().isoformat()
                break
            try:
                date.fromisoformat(v)
                args.start_date = v
            except ValueError:
                print("  use YYYY-MM-DD", flush=True)
        while args.sip_day is None:
            v = input(f"SIP debit day of month [{date.fromisoformat(args.start_date).day}]: ").strip()
            if not v:
                args.sip_day = min(date.fromisoformat(args.start_date).day, MAX_SIP_DAY)
                break
            try:
                d = int(v)
                args.sip_day = d if MIN_SIP_DAY <= d <= MAX_SIP_DAY else None
            except ValueError:
                args.sip_day = None
            if args.sip_day is None:
                print(f"  enter a day between {MIN_SIP_DAY} and {MAX_SIP_DAY}",
                      flush=True)


def main():
    ap = argparse.ArgumentParser(
        description="Stage 4 — exact percent + rupee allocation across the "
                    "verified finalists, from amount / risk / duration")
    ap.add_argument("--report", required=True,
                    help="recommendations.json from mf_recommend.py (Stage 2)")
    ap.add_argument("--nav-check",
                    help="nav_rolling_check.json from Stage 3 (default: the "
                         "file next to the report, if present)")
    ap.add_argument("--frequency", choices=FREQUENCIES, default="lumpsum",
                    help="'lumpsum' (one-time) or 'sip' (recurring monthly) "
                         "— default lumpsum")
    ap.add_argument("--amount", type=int,
                    help="lumpsum total, OR the monthly SIP installment "
                         "amount when --frequency sip, in whole rupees "
                         "(prompted when omitted)")
    ap.add_argument("--risk", choices=RISK_PROFILES,
                    help="risk appetite (prompted when omitted)")
    ap.add_argument("--years", type=float,
                    help="intended holding duration in years (prompted when "
                         "omitted)")
    ap.add_argument("--sip-day", type=int,
                    help=f"SIP only: recurring debit day of month "
                         f"({MIN_SIP_DAY}-{MAX_SIP_DAY}, avoids short-month "
                         f"ambiguity); prompted when omitted with --frequency sip")
    ap.add_argument("--start-date",
                    help="SIP only: first installment date YYYY-MM-DD "
                         "(default: today); recorded in the plan so Stage 5 "
                         "can reconstruct every installment")
    ap.add_argument("--step", type=int, default=1000,
                    help="amount granularity in rupees (default 1000): every "
                         "figure is a multiple of this, practical to place as "
                         "an order; any sub-step residue of a non-round total "
                         "goes to the largest allocation (noted in the plan). "
                         "--step 1 gives exact rupee-level splits")
    ap.add_argument("--allow-failed", action="store_true",
                    help="allocate even to picks that FAILED Stage 3 "
                         "(downgrades the block to a recorded warning)")
    ap.add_argument("--out",
                    help="output dir for allocation_plan.json/.md "
                         "(default: the report's dir)")
    args = ap.parse_args()
    if args.frequency == "sip" and args.sip_day is not None \
            and not (MIN_SIP_DAY <= args.sip_day <= MAX_SIP_DAY):
        ap.error(f"--sip-day must be between {MIN_SIP_DAY} and {MAX_SIP_DAY} "
                 f"(avoids short-month ambiguity)")
    if args.start_date:
        try:
            date.fromisoformat(args.start_date)
        except ValueError:
            ap.error(f"--start-date must be YYYY-MM-DD, got {args.start_date!r}")
    if args.frequency == "lumpsum" and (args.sip_day or args.start_date):
        ap.error("--sip-day / --start-date apply only to --frequency sip")
    _prompt_missing(args)

    with open(args.report, encoding="utf-8") as f:
        rep = json.load(f)
    picks = rep.get("recommendations", [])

    nav_path = args.nav_check or os.path.join(
        os.path.dirname(os.path.abspath(args.report)), "nav_rolling_check.json")
    nav_results = None
    if os.path.exists(nav_path):
        with open(nav_path, encoding="utf-8") as f:
            nav_results = json.load(f).get("results", [])
    blockers, nav_warnings = stage3_blockers(picks, nav_results,
                                             args.allow_failed)
    if blockers:
        for b in blockers:
            print(f"BLOCKED: {b}", flush=True)
        print("no plan written — resolve the failed pick(s) first "
              "(or pass --allow-failed to override).", flush=True)
        return 1

    try:
        plan = build_allocation(picks, args.risk, args.years, args.amount,
                                step=max(1, args.step))
    except ValueError as e:
        print(f"REFUSED: {e}", flush=True)
        return 1
    plan["warnings"] = nav_warnings + plan["warnings"]

    out_dir = args.out or os.path.dirname(os.path.abspath(args.report))
    os.makedirs(out_dir, exist_ok=True)
    inputs = {"frequency": args.frequency, "amount_inr": args.amount,
              "risk": args.risk, "duration_years": args.years,
              "horizon_band": plan["band"],
              "amount_step_inr": max(1, args.step)}
    if args.frequency == "sip":
        inputs["sip_day"] = args.sip_day
        inputs["sip_start_date"] = args.start_date
    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "inputs": inputs,
        "bucket_weights_used": plan["weights_used"],
        "allocation": plan["rows"],
        "warnings": plan["warnings"],
        "source_report_run_hash": rep.get("run_hash"),
        "note": ("Deterministic arithmetic over your own inputs — not "
                 "investment advice. Weights come from the explicit "
                 "risk x horizon template table in mf_allocate.py; amounts "
                 "sum exactly to the input" + (
                 f" (each fund's amount is its RECURRING monthly SIP "
                 f"installment, debited on day {args.sip_day} of each "
                 f"month starting {args.start_date} — Stage 5 reconstructs "
                 f"the full installment history from this contract)"
                 if args.frequency == "sip" else "") + ". Review the "
                 "per-fund manual verification checklist in "
                 "recommendations.md before placing orders, and rebalance "
                 "~annually."),
    }
    out["plan_hash"] = sha256_obj({k: v for k, v in out.items()
                                   if k != "generated_at"})
    jpath = os.path.join(out_dir, "allocation_plan.json")
    with open(jpath, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, sort_keys=True, ensure_ascii=False)

    amount_col = "Monthly SIP (₹)" if args.frequency == "sip" else "Amount (₹)"
    amount_line = (f"₹{args.amount:,}/month via SIP (day {args.sip_day}, "
                   f"starting {args.start_date})" if args.frequency == "sip"
                   else f"₹{args.amount:,} lumpsum")
    lines = ["# Investment Allocation Plan", "",
             f"- {amount_line}  |  risk: {args.risk}  |  "
             f"duration: {args.years:g}y (band {plan['band']})",
             f"- engine report: `{(rep.get('run_hash') or '')[:16]}…`  |  "
             f"plan_hash: `{out['plan_hash'][:16]}…`", "",
             f"| Fund | Bucket | % | {amount_col} |", "|---|---|---|---|"]
    for r in plan["rows"]:
        lines.append(f"| {r['fund']} | {r['bucket']} | {r['pct']} | "
                     f"{r['amount_inr']:,} |")
    lines += ["| **Total** | | **100** | "
              f"**{sum(r['amount_inr'] for r in plan['rows']):,}** |", ""]
    if plan["warnings"]:
        lines.append("### Warnings")
        lines += [f"- ⚠ {w}" for w in plan["warnings"]]
        lines.append("")
    lines.append(f"_{out['note']}_")
    with open(os.path.join(out_dir, "allocation_plan.md"), "w",
              encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print(f"Allocation ({args.risk}, {args.years:g}y -> band {plan['band']}, "
          f"{amount_line}):", flush=True)
    for r in plan["rows"]:
        print(f"  {r['pct']:>6}%  ₹{r['amount_inr']:>12,}  {r['fund']} "
              f"({r['bucket']})", flush=True)
    for w in plan["warnings"]:
        print(f"  ⚠ {w}", flush=True)
    print(f"plan -> {jpath} (+ allocation_plan.md)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
