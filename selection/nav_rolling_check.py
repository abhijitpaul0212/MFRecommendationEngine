#!/usr/bin/env python3
"""
nav_rolling_check.py — NAV rolling-return SECOND OPINION for the engine's
finalists. Closes the "lucky window" gap the Morningstar-snapshot engine
cannot: mf_recommend.py sees three point-in-time horizons (3Y/5Y/10Y as of
today), while this script recomputes CAGR over EVERY rolling window in the
fund's full NAV history — a fund whose alpha exists only because today's
window endpoints are favourable fails here.

Position in the process (README "Post-engine verification"):
  mf_recommend.py -> recommendations.json -> THIS SCRIPT -> invest / swap pick

With --report it checks the PICKS and (by default) the report's `bench` of
pre-validated substitutes in the same pass, so a failed pick already has a
verified replacement — no second iteration. The exit code gates on the picks
only; bench verdicts are contingency information. On a pick FAIL: promote a
PASSing substitute from that pick's bench, or re-run the engine with
--exclude 'failed fund' for a full constraint-checked re-selection.

What it does, per finalist:
  1. resolves the fund to an AMFI scheme code via the mfapi.in search API
     (community JSON mirror of AMFI's official daily NAV feed) — a wrong or
     ambiguous match is REPORTED and the fund marked UNRESOLVED, never guessed;
     pin codes explicitly with --map 'Fund Name=schemeCode'
  2. downloads the full daily NAV history for that scheme code
  3. feeds it through the DORMANT framework's tested math (mf_select.py:
     rolling_cagrs, sortino) and applies ITS gate thresholds, so the two
     frameworks can never drift apart:
       - rolling 3Y windows: >= rolling3y_pct_positive_min % must have CAGR > 0
       - rolling 5Y windows: the WORST window CAGR must be >= rolling5y_worst_min
       - Sortino 5Y: reported (informational — no category composite is fetched,
         so top-quartile-vs-peers remains a manual check on Value Research)
  4. prints a verdict per fund and writes nav_rolling_check.json next to the
     recommendations report.

Verdicts (honest by construction — never fabricates a pass):
  PASS                  both rolling gates hold on the full NAV history
  FAIL                  a rolling gate failed -> use a PASSing bench substitute
                        or re-run the engine with --exclude
  SHORT_HISTORY         genuinely YOUNG fund: complete daily NAVs, fresh latest
                        NAV, but fewer than min_history_years of life. Not a
                        data problem — an unproven fund; accept it manually or
                        prefer a seasoned peer (common for post-2020 launches)
  INCOMPLETE_HISTORY    the DATA looks wrong, not the fund's age: sparse (far
                        below the ~250/yr daily cadence -> truncated download)
                        or stale (latest NAV months old -> merged/dead scheme
                        or a wrong scheme-code match). Investigate / --map
  UNRESOLVED            could not confidently map the name to a scheme code;
                        re-run with --map

The SHORT_HISTORY vs INCOMPLETE_HISTORY split answers "is this a genuinely new
fund, or is history missing?" — a new fund is a legitimate finding to weigh;
missing data is a tooling issue to fix. Both stay non-PASS: the check never
passes a fund it cannot fully evaluate.

This is a LIVE-DATA tool: NAVs update daily, so unlike mf_recommend.py it is
deterministic only for a given download (each payload's SHA-256 and date range
are recorded in the output for auditability). Exit code is 0 only when every
finalist PASSes — safe to use as a gate in automation.

Usage
=====
  python selection/nav_rolling_check.py --report mf_out/recommendations.json
  python selection/nav_rolling_check.py --fund "Axis Small Cap Fund Direct Growth"
  python selection/nav_rolling_check.py --report mf_out/recommendations.json \
      --map "HDFC Flexi Cap Fund -Direct Plan - Growth Option=118955"

Stdlib only (urllib) — no new dependencies, same policy as the two engines.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mf_select import (DEFAULT_CONFIG as MFS_CONFIG, r6, rolling_cagrs,  # noqa: E402
                       sha256_obj, sortino)

MFAPI_SEARCH = "https://api.mfapi.in/mf/search?q={query}"
MFAPI_SCHEME = "https://api.mfapi.in/mf/{code}"
HTTP_TIMEOUT = 30

# Signals that separate a genuinely YOUNG fund (complete data, just short life)
# from a fund whose HISTORY IS INCOMPLETE (partial/wrong download). Indian MF
# NAVs publish daily (~250/yr), so a healthy fund of any age is dense and its
# latest NAV is a few days old at most.
STALE_NAV_MAX_DAYS = 30        # latest NAV older than this -> merged/dead scheme
                               # or a wrong scheme-code match, not a data lag
MIN_NAV_POINTS_PER_YEAR = 100  # far below the ~250/yr daily cadence -> the
                               # download is truncated, not the fund's real life

# Scheme-name tokens that identify a plan variant we must never match — the
# engine recommends Direct+Growth only, so its NAV twin must be the same plan.
EXCLUDED_PLAN_TOKENS = ("regular", "idcw", "dividend", "bonus", "payout",
                        "reinvest", "segregated")
REQUIRED_PLAN_TOKENS = ("direct", "growth")
# Generic tokens that carry no identity — ignored when matching fund names.
NOISE_TOKENS = frozenset({"fund", "plan", "option", "direct", "growth",
                          "scheme", "the", "of", "an", "a", "opt"})


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested, no network)
# ---------------------------------------------------------------------------

def tokenize(name):
    """Lower-cased alphanumeric tokens of a fund/scheme name."""
    return [t for t in re.split(r"[^a-z0-9]+", (name or "").lower()) if t]


def core_tokens(fund_name):
    """The identity-carrying tokens of a fund name (noise stripped) — the
    AMFI scheme we match MUST contain every one of these."""
    return frozenset(tokenize(fund_name)) - NOISE_TOKENS


def match_scheme(fund_name, candidates):
    """Pick the one AMFI scheme that IS this fund's Direct-Growth plan.

    candidates: [{"schemeCode": int, "schemeName": str}, ...] from mfapi search.
    Returns (schemeCode, schemeName, None) on a confident match, else
    (None, None, [eligible candidate names]) — ambiguity is surfaced for a
    --map override, never resolved by guessing. Deterministic throughout."""
    need = core_tokens(fund_name)
    eligible = []
    for c in candidates or []:
        toks = set(tokenize(c.get("schemeName")))
        if not all(t in toks for t in REQUIRED_PLAN_TOKENS):
            continue
        if any(t in toks for t in EXCLUDED_PLAN_TOKENS):
            continue
        if not need <= toks:            # every identity token must be present
            continue
        extra = len((toks - NOISE_TOKENS) - need)   # fewer stray tokens = closer
        eligible.append((extra, str(c.get("schemeCode")), c.get("schemeName")))
    if not eligible:
        return None, None, []
    eligible.sort()                     # (extra, code, name) — total order
    if len(eligible) > 1 and eligible[0][0] == eligible[1][0]:
        return None, None, [f"{code}: {name}" for _, code, name in eligible]
    _, code, name = eligible[0]
    return code, name, None


def parse_mfapi_history(payload):
    """mfapi scheme payload -> ascending [(date, nav_float)] series.
    Malformed rows, non-positive NAVs and duplicate dates are dropped
    (first occurrence wins — mfapi lists newest first)."""
    by_date = {}
    for row in (payload or {}).get("data") or []:
        try:
            d = datetime.strptime(row["date"], "%d-%m-%Y").date()
            v = float(row["nav"])
        except (KeyError, TypeError, ValueError):
            continue
        if v > 0 and d not in by_date:
            by_date[d] = v
    return sorted(by_date.items())


def evaluate_series(series, gates=None, rf=None, min_history_years=None,
                    as_of=None):
    """Rolling-return verdict for one NAV series, using the dormant
    framework's math and ITS thresholds (single source of truth). Pure and
    deterministic: same series in, same verdict out. Never fabricates a pass.
    Distinguishes a genuinely young fund (SHORT_HISTORY — complete data, just
    a short life) from a fund whose data looks partial or dead
    (INCOMPLETE_HISTORY — sparse or stale). `as_of` (the download date) enables
    the staleness test; without it staleness is not judged."""
    gates = gates or MFS_CONFIG["gates"]
    rf = MFS_CONFIG["risk_free_rate"] if rf is None else rf
    min_years = (MFS_CONFIG["universe"]["min_history_years"]
                 if min_history_years is None else min_history_years)
    out = {"nav_points": len(series), "history_years": None,
           "points_per_year": None, "last_nav_age_days": None,
           "first_date": None, "last_date": None,
           "rolling3y": None, "rolling5y": None, "sortino_5y": None,
           "verdict": "INCOMPLETE_HISTORY", "history_note": None}
    if len(series) < 2:
        out["history_note"] = "no usable NAV history returned"
        return out
    first_d, last_d = series[0][0], series[-1][0]
    years = r6((last_d - first_d).days / 365.25)
    ppy = r6(len(series) / years) if years > 0 else None
    age_days = (as_of - last_d).days if as_of else None
    out.update({"history_years": years, "points_per_year": ppy,
                "last_nav_age_days": age_days,
                "first_date": first_d.isoformat(),
                "last_date": last_d.isoformat()})
    # STALE (any age): the feed stopped long ago -> merged/closed scheme or a
    # wrong scheme-code match. Not investable on a PASS; flag for --map, never
    # evaluate as if current.
    if age_days is not None and age_days > STALE_NAV_MAX_DAYS:
        out["history_note"] = (
            f"latest NAV is {age_days} days old (> {STALE_NAV_MAX_DAYS}); the "
            f"scheme feed looks stale/dead or the code is a wrong match — "
            f"verify with --map rather than trusting this")
        return out          # verdict stays INCOMPLETE_HISTORY
    sparse = years >= 0.5 and ppy is not None and ppy < MIN_NAV_POINTS_PER_YEAR
    r3, r5 = rolling_cagrs(series, 3), rolling_cagrs(series, 5)
    if years < min_years or not r3 or not r5:
        if sparse:
            out["history_note"] = (
                f"only {out['nav_points']} NAVs over {years}y (~{ppy}/yr vs the "
                f"~250/yr daily cadence) — the download looks partial, not a "
                f"genuinely short life; re-download or check the scheme code")
            return out      # verdict stays INCOMPLETE_HISTORY
        out["verdict"] = "SHORT_HISTORY"
        out["history_note"] = (
            f"fund has {years}y of complete daily NAVs (< {min_years}y needed "
            f"for rolling 5Y windows) — a genuinely young fund, not a data "
            f"gap; it is simply unproven over a full market cycle, so decide "
            f"manually whether to accept it")
        return out
    pct_pos3 = r6(100.0 * sum(1 for c in r3 if c > 0) / len(r3))
    worst5 = r6(min(r5))
    med = sorted(r5)
    median5 = r6((med[len(med) // 2] if len(med) % 2
                  else (med[len(med) // 2 - 1] + med[len(med) // 2]) / 2.0))
    g3, g5 = gates["rolling3y_pct_positive_min"], gates["rolling5y_worst_min"]
    p3, p5 = pct_pos3 >= g3, worst5 >= g5
    out["rolling3y"] = {"windows": len(r3), "pct_positive": pct_pos3,
                        "threshold_min": g3, "passed": p3}
    out["rolling5y"] = {"windows": len(r5), "worst_cagr": worst5,
                        "median_cagr": median5, "threshold_min": g5,
                        "passed": p5}
    s5 = sortino(series, rf, 5)
    out["sortino_5y"] = r6(s5) if s5 is not None else None
    out["verdict"] = "PASS" if (p3 and p5) else "FAIL"
    return out


# ---------------------------------------------------------------------------
# Thin network layer (not unit-tested; everything above is)
# ---------------------------------------------------------------------------

def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "mf-nav-check/1.0"})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def resolve_and_evaluate(fund_name, overrides, as_of=None):
    """Full per-fund flow: resolve scheme code, download history, evaluate.
    Never raises on a per-fund problem — the verdict says what went wrong.
    `as_of` (today's date) drives the stale-NAV test in evaluate_series."""
    result = {"fund": fund_name, "scheme_code": None, "scheme_name": None,
              "source": None, "payload_sha256": None,
              "verdict": "UNRESOLVED", "note": None}
    code = overrides.get(fund_name)
    if code:
        result["note"] = "scheme code pinned via --map"
    else:
        # search on the identity tokens — mfapi matches loosely, we match strictly
        query = " ".join(sorted(core_tokens(fund_name))) or fund_name
        try:
            candidates = fetch_json(
                MFAPI_SEARCH.format(query=urllib.parse.quote(query)))
        except Exception as e:
            result["note"] = f"search failed: {e}"
            return result
        code, matched_name, ambiguous = match_scheme(fund_name, candidates)
        if code is None:
            result["note"] = (
                "no confident Direct-Growth match; candidates: "
                + ("; ".join(ambiguous) if ambiguous else "none")
                + " — re-run with --map 'Fund Name=schemeCode'")
            return result
        result["scheme_name"] = matched_name
    url = MFAPI_SCHEME.format(code=code)
    try:
        payload = fetch_json(url)
    except Exception as e:
        result["note"] = f"NAV download failed: {e}"
        return result
    result.update({
        "scheme_code": code, "source": url,
        "scheme_name": result["scheme_name"]
        or ((payload.get("meta") or {}).get("scheme_name")),
        "payload_sha256": sha256_obj(payload)})
    result.update(evaluate_series(parse_mfapi_history(payload), as_of=as_of))
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def finalists_from_report(rep, include_bench=True):
    """(fund, role) pairs to verify: every pick, then (unless disabled) every
    bench alternate — checking substitutes in the SAME pass means a failed
    pick already has a verified replacement waiting. Deduped (a fund can be
    on two picks' benches), picks always win the 'pick' role. Pure."""
    out, seen = [], set()
    for r in rep.get("recommendations", []):
        if r["fund"] not in seen:
            out.append((r["fund"], "pick"))
            seen.add(r["fund"])
    if include_bench:
        for b in rep.get("bench") or []:
            for a in b.get("alternates") or []:
                if a["fund"] not in seen:
                    out.append((a["fund"], f"bench for {b['pick']}"))
                    seen.add(a["fund"])
    return out


def load_finalists(report_path, include_bench=True):
    with open(report_path, encoding="utf-8") as f:
        rep = json.load(f)
    return finalists_from_report(rep, include_bench)


def main():
    ap = argparse.ArgumentParser(
        description="NAV rolling-return second opinion for the recommendation "
                    "engine's finalists (AMFI data via mfapi.in; thresholds "
                    "imported from mf_select.py)")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--report",
                     help="recommendations.json from mf_recommend.py — checks "
                          "every fund under `recommendations` PLUS the "
                          "pre-validated `bench` substitutes (see --no-bench)")
    src.add_argument("--fund", action="append",
                     help="check this fund name directly (repeatable)")
    ap.add_argument("--no-bench", action="store_true",
                    help="with --report: check only the picks, skip the bench "
                         "substitutes (default checks both, so a failed pick "
                         "already has a verified replacement)")
    ap.add_argument("--map", action="append", default=[],
                    metavar="'Fund Name=schemeCode'",
                    help="pin a fund to an AMFI scheme code, bypassing name "
                         "matching (repeatable)")
    ap.add_argument("--out",
                    help="output dir for nav_rolling_check.json (default: the "
                         "report's dir, or CWD with --fund)")
    args = ap.parse_args()

    overrides = {}
    for m in args.map:
        name, _, code = m.partition("=")
        if not code.strip():
            ap.error(f"--map needs 'Fund Name=schemeCode', got: {m!r}")
        overrides[name.strip()] = code.strip()

    if args.report:
        targets = load_finalists(args.report, include_bench=not args.no_bench)
    else:
        targets = [(name, "pick") for name in args.fund]
    if not targets:
        raise SystemExit("no finalists found to check")
    out_dir = args.out or (os.path.dirname(os.path.abspath(args.report))
                           if args.report else os.getcwd())

    n_picks = sum(1 for _, role in targets if role == "pick")
    print(f"NAV rolling-return check: {n_picks} pick(s) + "
          f"{len(targets) - n_picks} bench substitute(s); gates from "
          f"mf_select.py (3Y windows >= "
          f"{MFS_CONFIG['gates']['rolling3y_pct_positive_min']}% positive, "
          f"worst 5Y window >= {MFS_CONFIG['gates']['rolling5y_worst_min']})",
          flush=True)
    today = datetime.now(timezone.utc).date()
    results = []
    for name, role in targets:
        r = resolve_and_evaluate(name, overrides, as_of=today)
        r["role"] = role
        results.append(r)
    for r in results:
        tag = "" if r["role"] == "pick" else f"  ({r['role']})"
        if r["verdict"] in ("PASS", "FAIL"):
            r3, r5 = r["rolling3y"], r["rolling5y"]
            print(f"  [{r['verdict']:4s}] {r['fund']}{tag}\n"
                  f"         3Y: {r3['pct_positive']}% of {r3['windows']} "
                  f"windows positive (need >= {r3['threshold_min']}%) "
                  f"{'OK' if r3['passed'] else 'FAIL'}\n"
                  f"         5Y: worst {r5['worst_cagr']:+.4f}, median "
                  f"{r5['median_cagr']:+.4f} over {r5['windows']} windows "
                  f"(worst must be >= {r5['threshold_min']}) "
                  f"{'OK' if r5['passed'] else 'FAIL'}\n"
                  f"         Sortino 5Y: {r['sortino_5y']} (informational; "
                  f"top-quartile-vs-category stays a manual check)", flush=True)
        else:
            # SHORT_HISTORY / INCOMPLETE_HISTORY carry a history_note; UNRESOLVED
            # carries a resolution note
            print(f"  [{r['verdict']}] {r['fund']}{tag}: "
                  f"{r.get('history_note') or r.get('note') or 'not evaluable'}",
                  flush=True)

    os.makedirs(out_dir, exist_ok=True)
    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "AMFI daily NAVs via api.mfapi.in",
        "thresholds": {
            "rolling3y_pct_positive_min":
                MFS_CONFIG["gates"]["rolling3y_pct_positive_min"],
            "rolling5y_worst_min": MFS_CONFIG["gates"]["rolling5y_worst_min"],
            "min_history_years": MFS_CONFIG["universe"]["min_history_years"],
            "risk_free_rate": MFS_CONFIG["risk_free_rate"]},
        "note": ("Live-data second opinion on the engine's finalists — NAVs "
                 "update daily, so this is reproducible per download (see "
                 "payload_sha256 + date range per fund), not across days. "
                 "A pick FAIL: use a PASSing bench substitute from the same "
                 "pick's bench (already verified here), or re-run the engine "
                 "with --exclude 'failed fund' for a full re-selection. "
                 "Bench verdicts never affect the exit code."),
        "results": results,
    }
    path = os.path.join(out_dir, "nav_rolling_check.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, sort_keys=True, ensure_ascii=False)
    verdicts = [r["verdict"] for r in results]
    pick_verdicts = [r["verdict"] for r in results if r["role"] == "pick"]
    print(f"report -> {path}", flush=True)
    print(f"summary: {verdicts.count('PASS')} PASS, {verdicts.count('FAIL')} "
          f"FAIL, {verdicts.count('SHORT_HISTORY')} short-history (young fund), "
          f"{verdicts.count('INCOMPLETE_HISTORY')} incomplete-history (data "
          f"issue), {verdicts.count('UNRESOLVED')} unresolved "
          f"(exit code gates on the {len(pick_verdicts)} pick(s) only)",
          flush=True)
    return 0 if all(v == "PASS" for v in pick_verdicts) else 1


if __name__ == "__main__":
    raise SystemExit(main())
