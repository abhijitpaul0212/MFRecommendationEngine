#!/usr/bin/env python3
"""
mf_rebalance.py — STAGE 5 (periodic): audit a LIVE portfolio against the
buy-time contract (allocation_plan.json) and today's quality machinery, and
say — with the reasoning written out — whether to HOLD, REBALANCE (exact buy/
sell amounts), or REPLACE a fund.

Position in the process (README quick guide):
  months later:  fresh Stage 1 scrape -> fresh Stage 2 report -> THIS SCRIPT
  -> rebalance_plan.json / rebalance_plan.md

Two independent families of trigger, mirroring how the money was invested:

1. DRIFT (arithmetic vs the buy-time plan) — the classic 5/25 rule: a fund
   breaches when its current weight is off target by more than --drift-pp
   absolute percentage points (default 5) OR by more than --drift-rel
   percent of its target (default 25%). Current values are derived
   FREQUENCY-AWARE from the plan's own contract (allocation_plan.json
   records whether it was a LUMPSUM or a recurring SIP):
     - lumpsum: units = amount / NAV on the buy date; value = units x latest
       NAV (wrong for a SIP — many purchase dates, many NAVs);
     - sip: the plan's sip_day + sip_start_date are replayed into every
       monthly installment date through today, each priced at ITS OWN NAV,
       and units are summed — the correct SIP valuation;
   or supplied exactly with --current 'Fund=value' (you know the value
   better than any derivation), or precisely with --transactions FILE for
   irregular SIPs (missed/changed installments) or a lumpsum top-up mixed
   into a SIP — every number shown in the output either way.

2. QUALITY (the same machinery the initial investment went through):
   - fresh Stage 2 gates: a held fund now in `excluded_by_gates` of the NEW
     report FAILS quality (the exact failed checks are quoted); a fund
     missing from the new snapshot entirely is INCONCLUSIVE (rescrape —
     never guessed);
   - Stage 3 rolling-return check re-run live on every held fund (same
     gates, same math);
   - pairwise overlap between HELD funds recomputed from the fresh scrape
     (--data), warned when the <=10% line is crossed.

Decision precedence (quality outranks arithmetic — a portfolio is never
"rebalanced" into a fund that no longer deserves the money):
  REPLACEMENT_REQUIRED  any held fund fails fresh gates or the rolling check.
                        No trades are computed — first fix composition via the
                        engine loop (Stage 2 --exclude 'failed fund' -> Stage 3
                        -> Stage 4 on the proceeds), then re-run this stage.
  REBALANCE_REQUIRED    quality intact but a drift threshold breached ->
                        exact whole-rupee buy/sell per fund restoring the plan
                        weights. With --new-money N the targets are computed
                        over (current + N); when N is large enough the plan
                        becomes pure buys — the tax-efficient path (selling
                        equity funds realises STCG/LTCG; directing fresh money
                        does not).
  HOLD                  within thresholds, quality intact — do nothing, and
                        the output says exactly why.

Honesty properties: every verdict carries its numbers (weight vs target,
drift in pp and %, failed gate names, rolling windows); NAV payload hashes +
date ranges recorded; INCONCLUSIVE is a real verdict — missing data blocks
trades rather than being papered over. Not investment advice.

Usage
=====
  python selection/mf_rebalance.py \
      --plan recommendation_run/allocation_plan.json \
      --report recommendation_run/recommendations.json \
      --data ms_data
  # options: --buy-date 2026-07-07  --sip-day 5  --as-of 2026-07-01
  #          --current 'Fund Name=1234567' (repeatable)
  #          --transactions purchases.json
  #          --new-money 200000  --drift-pp 5 --drift-rel 25
  #          --map 'Fund Name=schemeCode'  --skip-rolling  --out DIR

Stdlib only; reuses the tested math of nav_rolling_check / mf_select /
mf_recommend so no threshold or formula exists twice.
"""

from __future__ import annotations

import argparse
import calendar
import glob
import json
import os
import sys
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mf_select import pairwise_overlap, r6, sha256_obj  # noqa: E402
from mf_recommend import equity_weights  # noqa: E402
import nav_rolling_check as nrc  # noqa: E402

DRIFT_PP_DEFAULT = 5.0     # absolute percentage-point drift threshold
DRIFT_REL_DEFAULT = 25.0   # relative drift threshold, % of target weight
OVERLAP_WARN_PCT = 10.0    # same line the engine selects under


# ---------------------------------------------------------------------------
# Pure core (unit-tested, no I/O)
# ---------------------------------------------------------------------------

def nav_on_or_before(series, d):
    """Latest (date, nav) at or before d from an ascending series; None if the
    series starts after d."""
    best = None
    for sd, v in series:
        if sd > d:
            break
        best = (sd, v)
    return best


def derive_current_value(amount_inr, series, buy_date):
    """LUMPSUM: one buy-time amount -> today's value via units held. Returns
    the full derivation {units, nav_buy(+date), nav_now(+date),
    current_value} or None when the series can't support it — never guesses.
    Wrong for a SIP (many purchase dates, many NAVs) — see
    derive_value_from_transactions / sip_installment_dates for that case."""
    if not series:
        return None
    buy = nav_on_or_before(series, buy_date)
    if buy is None or buy[1] <= 0:
        return None
    nav_buy_date, nav_buy = buy
    nav_now_date, nav_now = series[-1]
    units = amount_inr / nav_buy
    return {"units": r6(units), "nav_buy": nav_buy,
            "nav_buy_date": nav_buy_date.isoformat(),
            "nav_now": nav_now, "nav_now_date": nav_now_date.isoformat(),
            "current_value": int(round(units * nav_now))}


def sip_installment_dates(start, as_of, day):
    """One date per calendar month from `start` through `as_of`, clamped to
    `day` (or the month's last day if shorter, e.g. day=31 in February) —
    the same clamping a real SIP mandate uses. Only dates >= start and
    <= as_of are returned. Deterministic; day should be validated by the
    caller to 1-28 to sidestep short-month ambiguity."""
    if start > as_of:
        return []
    dates = []
    y, m = start.year, start.month
    while True:
        last = calendar.monthrange(y, m)[1]
        d = date(y, m, min(day, last))
        if d > as_of:
            break
        if d >= start:
            dates.append(d)
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return dates


def derive_value_from_transactions(transactions, series):
    """GENERAL CASE (SIP, staggered lumpsum, top-ups): sums units bought at
    the NAV on-or-before EACH transaction's own date, then values the total
    at today's NAV. transactions: [(date, amount)] in any order. Returns the
    full derivation (units, nav_now(+date), current_value, total_invested,
    installment count, per-transaction breakdown) or None if the series is
    empty or ANY transaction predates the earliest available NAV — never
    guesses a price for money that couldn't have bought units yet."""
    if not series or not transactions:
        return None
    total_units, breakdown = 0.0, []
    for d, amt in sorted(transactions):
        hit = nav_on_or_before(series, d)
        if hit is None or hit[1] <= 0:
            return None
        nav_date, nav = hit
        units = amt / nav
        total_units += units
        breakdown.append({"date": d.isoformat(), "amount": amt, "nav": nav,
                          "nav_date": nav_date.isoformat(), "units": r6(units)})
    nav_now_date, nav_now = series[-1]
    return {"units": r6(total_units), "nav_now": nav_now,
            "nav_now_date": nav_now_date.isoformat(),
            "current_value": int(round(total_units * nav_now)),
            "total_invested": sum(a for _, a in transactions),
            "installments": len(transactions), "transactions": breakdown}


def resolve_fund_value(plan_amount, frequency, sip_day, anchor_date, today,
                       series, current_override=None, txn_override=None):
    """The single decision point for 'what is this fund worth today',
    honoring the SAME priority for every fund: an exact --current override
    always wins (you know better than any derivation); a --transactions
    override covers irregular/staggered/topped-up cases (SIP that changed
    amount, missed a month, or mixed with a lumpsum top-up) precisely;
    otherwise the plan's OWN contract is replayed automatically — its SIP
    schedule (frequency='sip') or its single buy date (frequency='lumpsum').
    Returns the derivation dict (with a 'method' tag) or None -> caller
    reports INCONCLUSIVE rather than guessing. Pure: series is already
    downloaded, today is passed in, no network/clock access here."""
    if current_override is not None:
        return {"current_value": current_override,
                "derivation": "user --current override", "method": "override"}
    if txn_override is not None:
        d = derive_value_from_transactions(txn_override, series)
        if d is not None:
            d["method"] = "transactions"
        return d
    if frequency == "sip":
        if sip_day is None or anchor_date is None:
            return None
        dates = sip_installment_dates(anchor_date, today, sip_day)
        if not dates:
            return None
        d = derive_value_from_transactions(
            [(dt, plan_amount) for dt in dates], series)
        if d is not None:
            d["method"] = "sip_auto"
        return d
    if anchor_date is None:
        return None
    d = derive_current_value(plan_amount, series, anchor_date)
    if d is not None:
        d["method"] = "lumpsum_auto"
    return d


def compute_drift(targets_pct, current_values, pp_thr, rel_thr):
    """Per-fund drift vs the buy-time plan, 5/25-style dual threshold.
    targets_pct: {fund: pct}; current_values: {fund: rupees}. Returns rows
    sorted by fund with breach flags and the reasoning already worded."""
    total = sum(current_values.values())
    rows = []
    for fund in sorted(targets_pct):
        tgt = targets_pct[fund]
        cur = current_values[fund]
        cur_pct = r6(100.0 * cur / total) if total > 0 else 0.0
        dpp = r6(cur_pct - tgt)
        drel = r6(100.0 * dpp / tgt) if tgt > 0 else None
        b_pp = abs(dpp) > pp_thr
        b_rel = drel is not None and abs(drel) > rel_thr
        why = (f"weight {cur_pct}% vs target {tgt}% (drift {dpp:+}pp"
               + (f", {drel:+}% of target" if drel is not None else "") + ") — ")
        if b_pp or b_rel:
            trig = " and ".join(
                ([f"|{dpp}|pp > {pp_thr}pp"] if b_pp else [])
                + ([f"|{drel}|% > {rel_thr}%"] if b_rel else []))
            why += f"BREACH ({trig})"
        else:
            why += f"within the {pp_thr}pp / {rel_thr}% thresholds"
        rows.append({"fund": fund, "target_pct": tgt, "current_value": cur,
                     "current_pct": cur_pct, "drift_pp": dpp,
                     "drift_rel_pct": drel, "breach": b_pp or b_rel,
                     "reason": why})
    return rows


def rebalance_trades(current_values, targets_pct, new_money=0):
    """Whole-rupee deltas restoring target weights over (current + new_money).
    Positive = buy, negative = sell; deltas sum EXACTLY to new_money (largest
    remainder). Pure."""
    total = sum(current_values.values()) + new_money
    raw = {f: total * p / 100.0 - current_values[f]
           for f, p in targets_pct.items()}
    base = {f: int(raw[f] // 1) for f in raw}          # floor (toward -inf)
    leftover = new_money - sum(base.values())
    order = sorted(raw, key=lambda f: (-(raw[f] - base[f]), f))
    for f in order[:leftover]:
        base[f] += 1
    return base


def quality_status(held_funds, fresh_report):
    """Where each held fund stands in a FRESH Stage 2 report. Three honest
    states: gates PASS (in `ranking`), gates FAIL (in `excluded_by_gates`,
    failed checks quoted), or NOT_IN_SNAPSHOT (inconclusive — rescrape; never
    treated as a pass OR a fail). Also notes pick/bench presence. Pure."""
    ranking = {r["fund"]: r for r in fresh_report.get("ranking", [])}
    excluded = {e["fund"]: e for e in fresh_report.get("excluded_by_gates", [])}
    picks = {r["fund"] for r in fresh_report.get("recommendations", [])}
    bench = {a["fund"] for b in fresh_report.get("bench") or []
             for a in b.get("alternates") or []}
    out = {}
    for f in held_funds:
        if f in ranking:
            out[f] = {"status": "PASS_GATES",
                      "still_pick": f in picks, "on_bench": f in bench,
                      "reason": (f"passes every fresh gate (rank "
                                 f"{ranking[f]['rank']}, score "
                                 f"{ranking[f]['score']})"
                                 + ("; still a current pick" if f in picks
                                    else "; no longer the top pick but still a "
                                         "gate survivor — quality intact"))}
        elif f in excluded:
            checks = excluded[f]["failed_checks"]
            out[f] = {"status": "FAILS_GATES", "failed_checks": checks,
                      "still_pick": False, "on_bench": False,
                      "reason": (f"now FAILS fresh gate(s) {checks} — the "
                                 f"capital-protection quality it was bought "
                                 f"on no longer holds")}
        else:
            out[f] = {"status": "NOT_IN_SNAPSHOT",
                      "still_pick": False, "on_bench": False,
                      "reason": ("absent from the fresh snapshot (not scraped, "
                                 "renamed, or recategorised out of the "
                                 "universe) — INCONCLUSIVE; re-scrape before "
                                 "acting")}
    return out


def decide(drift_rows, quality, rolling_verdicts):
    """Per-fund action + portfolio verdict, quality outranking drift.
    rolling_verdicts: {fund: verdict-str} or {} when skipped. Pure."""
    actions, replace, inconclusive, breach = {}, [], [], []
    for row in drift_rows:
        f = row["fund"]
        q = quality.get(f, {})
        reasons = [f"drift: {row['reason']}",
                   f"fresh gates: {q.get('reason', 'no fresh report entry')}"]
        rv = rolling_verdicts.get(f)
        if rv is not None:
            reasons.append(f"rolling check: {rv}")
        if q.get("status") == "FAILS_GATES" or rv == "FAIL":
            actions[f] = {"action": "REPLACE", "reasons": reasons}
            replace.append(f)
        elif q.get("status") == "NOT_IN_SNAPSHOT":
            actions[f] = {"action": "INCONCLUSIVE", "reasons": reasons}
            inconclusive.append(f)
        elif row["breach"]:
            actions[f] = {"action": "TRIM" if row["drift_pp"] > 0 else "ADD",
                          "reasons": reasons}
            breach.append(f)
        else:
            actions[f] = {"action": "HOLD", "reasons": reasons}
    if replace:
        verdict = "REPLACEMENT_REQUIRED"
    elif inconclusive:
        verdict = "INCONCLUSIVE"
    elif breach:
        verdict = "REBALANCE_REQUIRED"
    else:
        verdict = "HOLD"
    return actions, verdict


# ---------------------------------------------------------------------------
# Thin I/O layer
# ---------------------------------------------------------------------------

def held_fund_overlaps(held_funds, data_dir):
    """Measured pairwise overlap between held funds from the fresh scrape's
    per-house files. Returns ({pair: pct}, [warnings])."""
    raws, warnings = {}, []
    if not (data_dir and os.path.isdir(data_dir)):
        return {}, [f"--data dir not found ({data_dir}) — held-fund overlap "
                    f"not re-checked"]
    for p in glob.glob(os.path.join(data_dir, "*.json")):
        if os.path.basename(p) in ("morningstar_factsheet.json", "filters.json"):
            continue
        try:
            data = json.load(open(p, encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for f in held_funds:
            if f in data and f not in raws:
                raws[f] = data[f]
    matrix = {}
    missing = [f for f in held_funds if f not in raws]
    if missing:
        warnings.append(f"no fresh holdings data for {missing} — their "
                        f"overlap not re-checked")
    names = [f for f in held_funds if f in raws]
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            wa, wb = equity_weights(raws[a]), equity_weights(raws[b])
            if wa and wb:
                ov = pairwise_overlap(wa, wb)
                matrix[f"{a} x {b}"] = ov
                if ov > OVERLAP_WARN_PCT:
                    warnings.append(
                        f"held-fund overlap {a} x {b} = {ov}% (> "
                        f"{OVERLAP_WARN_PCT}%) — the portfolios have "
                        f"converged since purchase; consider the engine loop")
    return matrix, warnings


def main():
    ap = argparse.ArgumentParser(
        description="Stage 5 — periodic rebalancing audit: drift vs the "
                    "buy-time plan + the full quality re-check, with reasons")
    ap.add_argument("--plan", required=True,
                    help="allocation_plan.json written at buy time (Stage 4)")
    ap.add_argument("--report", required=True,
                    help="FRESH recommendations.json (re-run Stages 1-2 "
                         "first: quality is judged on today's snapshot)")
    ap.add_argument("--data", default="ms_data",
                    help="fresh scrape dir, for held-fund overlap re-check")
    ap.add_argument("--buy-date",
                    help="anchor date override: the lumpsum buy date, OR the "
                         "SIP first-installment date (default: read from the "
                         "plan — its own generated_at date for lumpsum, or "
                         "its recorded sip_start_date for SIP)")
    ap.add_argument("--sip-day", type=int,
                    help="SIP debit day override (default: the plan's "
                         "recorded sip_day; ignored for lumpsum plans)")
    ap.add_argument("--as-of",
                    help="YYYY-MM-DD to audit as of (default: today) — also "
                         "the last date SIP installments are counted through")
    ap.add_argument("--current", action="append", default=[],
                    metavar="'Fund Name=rupees'",
                    help="exact current value override (repeatable) — highest "
                         "priority; use when you know the real value better "
                         "than any derivation")
    ap.add_argument("--transactions",
                    help="JSON file {fund: [{\"date\":\"YYYY-MM-DD\", "
                         "\"amount\":N}, ...]} of ACTUAL purchase dates/"
                         "amounts per fund — the precise fix for an "
                         "irregular SIP (missed/changed installments) or a "
                         "lumpsum top-up mixed into a SIP; overrides the "
                         "plan's auto-reconstructed schedule for listed funds")
    ap.add_argument("--new-money", type=int, default=0,
                    help="fresh amount to add; rebalance targets are computed "
                         "over (current + new) so large-enough N gives a "
                         "pure-buy, tax-friendlier plan")
    ap.add_argument("--drift-pp", type=float, default=DRIFT_PP_DEFAULT,
                    help=f"absolute drift threshold in percentage points "
                         f"(default {DRIFT_PP_DEFAULT})")
    ap.add_argument("--drift-rel", type=float, default=DRIFT_REL_DEFAULT,
                    help=f"relative drift threshold as %% of target "
                         f"(default {DRIFT_REL_DEFAULT})")
    ap.add_argument("--map", action="append", default=[],
                    metavar="'Fund Name=schemeCode'",
                    help="pin AMFI scheme codes (as in nav_rolling_check)")
    ap.add_argument("--skip-rolling", action="store_true",
                    help="skip the live Stage 3 rolling re-check (faster; "
                         "recorded as a warning)")
    ap.add_argument("--out", help="output dir (default: the plan's dir)")
    args = ap.parse_args()

    with open(args.plan, encoding="utf-8") as f:
        plan = json.load(f)
    with open(args.report, encoding="utf-8") as f:
        fresh = json.load(f)
    rows = plan["allocation"]
    held = [r["fund"] for r in rows]
    targets = {r["fund"]: r["pct"] for r in rows}
    pinputs = plan.get("inputs") or {}
    frequency = pinputs.get("frequency", "lumpsum")   # old plans lack this
    sip_day = args.sip_day if args.sip_day is not None else pinputs.get("sip_day")
    if args.buy_date:
        anchor_date = date.fromisoformat(args.buy_date)
    elif frequency == "sip" and pinputs.get("sip_start_date"):
        anchor_date = date.fromisoformat(pinputs["sip_start_date"])
    elif frequency == "lumpsum":
        anchor_date = datetime.fromisoformat(plan["generated_at"]).date()
    else:
        anchor_date = None      # SIP plan missing its own schedule
    today = (date.fromisoformat(args.as_of) if args.as_of
             else datetime.now(timezone.utc).date())

    overrides, mapov, txn_overrides = {}, {}, {}
    for item, dest, what in ((args.current, overrides, "--current"),
                             (args.map, mapov, "--map")):
        for m in item:
            name, _, val = m.partition("=")
            if not val.strip():
                ap.error(f"{what} needs 'Fund Name=value', got: {m!r}")
            dest[name.strip()] = val.strip()
    overrides = {k: int(v) for k, v in overrides.items()}
    if args.transactions:
        with open(args.transactions, encoding="utf-8") as f:
            raw_txns = json.load(f)
        for fund, items in raw_txns.items():
            txn_overrides[fund] = [(date.fromisoformat(t["date"]), t["amount"])
                                   for t in items]

    # per-fund NAV series: one download serves BOTH the value derivation and
    # the rolling re-check
    warnings, derivations, current_values, rolling = [], {}, {}, {}
    for r in rows:
        f = r["fund"]
        need_series = f not in overrides or not args.skip_rolling
        series, meta = [], {}
        if need_series:
            res = nrc.resolve_and_evaluate(f, mapov, as_of=today)
            meta = res
            if res.get("source"):
                payload = nrc.fetch_json(res["source"])
                series = nrc.parse_mfapi_history(payload)
            if not args.skip_rolling:
                rolling[f] = res["verdict"]
        d = resolve_fund_value(
            r["amount_inr"], frequency, sip_day, anchor_date, today, series,
            current_override=overrides.get(f), txn_override=txn_overrides.get(f))
        if d is None:
            hint = ("plan has no recorded SIP schedule (sip_day/"
                    "sip_start_date) — pass --sip-day and --buy-date"
                    if frequency == "sip" and (sip_day is None or anchor_date is None)
                    else meta.get("note") or "no NAV series")
            print(f"INCONCLUSIVE: cannot derive current value for "
                  f"'{f}' ({hint}) — supply --current '{f}=<value>' or "
                  f"--transactions", flush=True)
            return 1
        current_values[f] = d["current_value"]
        if "derivation" not in d:
            d["derivation"] = (
                f"SIP: {d.get('installments')} installment(s) of "
                f"₹{r['amount_inr']:,} from {anchor_date} on day {sip_day}"
                if d.get("method") == "sip_auto" else
                f"units = ₹{r['amount_inr']:,} / NAV {d.get('nav_buy')} on "
                f"{d.get('nav_buy_date')}" if d.get("method") == "lumpsum_auto"
                else f"{d.get('installments')} dated transaction(s) "
                     f"totalling ₹{d.get('total_invested', 0):,}")
        derivations[f] = d
    if args.skip_rolling:
        warnings.append("rolling re-check skipped (--skip-rolling) — quality "
                        "verdicts rest on fresh gates only")
    if frequency == "sip":
        warnings.append(
            "this portfolio is SIP-funded: for a REBALANCE_REQUIRED verdict, "
            "consider adjusting the NEXT installment's fund-wise split "
            "toward the target percentages instead of an immediate lump-sum "
            "buy/sell — the trades below are the immediate one-time alternative")

    drift_rows = compute_drift(targets, current_values,
                               args.drift_pp, args.drift_rel)
    quality = quality_status(held, fresh)
    actions, verdict = decide(drift_rows, quality, rolling)
    overlap_matrix, ov_warnings = held_fund_overlaps(held, args.data)
    warnings += ov_warnings

    trades = None
    if verdict == "REBALANCE_REQUIRED":
        trades = rebalance_trades(current_values, targets, args.new_money)
        if args.new_money and all(v >= 0 for v in trades.values()):
            warnings.append(f"new money ₹{args.new_money:,} fully absorbs the "
                            f"rebalance — pure buys, no sells, no capital "
                            f"gains realised")
        elif any(v < 0 for v in trades.values()):
            warnings.append("plan includes SELLS — realising gains has "
                            "STCG/LTCG consequences; consider --new-money to "
                            "rebalance with fresh cash instead")

    out_dir = args.out or os.path.dirname(os.path.abspath(args.plan))
    os.makedirs(out_dir, exist_ok=True)
    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "verdict": verdict,
        "inputs": {"frequency": frequency,
                   "anchor_date": anchor_date.isoformat() if anchor_date else None,
                   "sip_day": sip_day if frequency == "sip" else None,
                   "as_of": today.isoformat(),
                   "drift_pp_threshold": args.drift_pp,
                   "drift_rel_threshold_pct": args.drift_rel,
                   "new_money_inr": args.new_money,
                   "source_plan_hash": plan.get("plan_hash"),
                   "fresh_report_run_hash": fresh.get("run_hash")},
        "portfolio": drift_rows,
        "value_derivations": derivations,
        "actions": actions,
        "trades_inr": trades,
        "held_overlap_pct": overlap_matrix,
        "warnings": warnings,
        "note": ("Quality outranks arithmetic: REPLACE/INCONCLUSIVE funds "
                 "block trade computation — fix composition through the "
                 "engine loop (Stage 2 --exclude -> 3 -> 4), then re-run. "
                 "Deterministic per NAV download. Not investment advice."),
    }
    out["rebalance_hash"] = sha256_obj({k: v for k, v in out.items()
                                        if k != "generated_at"})
    jpath = os.path.join(out_dir, "rebalance_plan.json")
    with open(jpath, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, sort_keys=True, ensure_ascii=False)

    schedule_label = (f"SIP day {sip_day} from {anchor_date}" if frequency == "sip"
                      else f"buy date {anchor_date}")
    lines = ["# Rebalancing Audit", "",
             f"**Verdict: {verdict}**  ({frequency}, {schedule_label}, "
             f"thresholds {args.drift_pp}pp / {args.drift_rel}%)", ""]
    for row in drift_rows:
        a = actions[row["fund"]]
        lines += [f"## {a['action']} — {row['fund']}",
                  f"₹{row['current_value']:,} today ({row['current_pct']}% vs "
                  f"target {row['target_pct']}%)", ""]
        lines += [f"- {reason}" for reason in a["reasons"]]
        lines.append("")
    if trades:
        lines += ["### Trades to restore the plan"
                  + (f" (including ₹{args.new_money:,} new money)"
                     if args.new_money else ""),
                  "| Fund | Action | Amount (₹) |", "|---|---|---|"]
        for f in sorted(trades):
            v = trades[f]
            lines.append(f"| {f} | {'BUY' if v >= 0 else 'SELL'} | "
                         f"{abs(v):,} |")
        lines.append("")
    if verdict == "REPLACEMENT_REQUIRED":
        bad = sorted(f for f, a in actions.items() if a["action"] == "REPLACE")
        lines += ["### Next steps (composition first, then weights)",
                  f"1. Re-run Stage 2 with "
                  + " ".join(f"--exclude '{f}'" for f in bad),
                  "2. Verify the new picks + bench with Stage 3",
                  "3. Allocate the sale proceeds with Stage 4",
                  "4. Re-run this stage against the new plan", ""]
    if warnings:
        lines += ["### Warnings"] + [f"- ⚠ {w}" for w in warnings] + [""]
    lines.append(f"_{out['note']}_")
    with open(os.path.join(out_dir, "rebalance_plan.md"), "w",
              encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print(f"VERDICT: {verdict}", flush=True)
    for row in drift_rows:
        a = actions[row["fund"]]
        print(f"  [{a['action']:<12}] {row['fund']}: {row['reason']}",
              flush=True)
        for reason in a["reasons"][1:]:
            print(f"                 {reason}", flush=True)
    if trades:
        for f in sorted(trades):
            v = trades[f]
            print(f"  TRADE: {'BUY ' if v >= 0 else 'SELL'} ₹{abs(v):>12,}  "
                  f"{f}", flush=True)
    for w in warnings:
        print(f"  ⚠ {w}", flush=True)
    print(f"plan -> {jpath} (+ rebalance_plan.md)", flush=True)
    return 0 if verdict == "HOLD" else 1


if __name__ == "__main__":
    raise SystemExit(main())
