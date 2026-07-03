#!/usr/bin/env python3
"""
mf_recommend.py — Deterministic MF recommendation engine over the enriched
Morningstar per-fund JSON produced by the scraper pipeline
(scraper/morningstar_factsheet.py + scraper/morningstar_fund_details.py).

Every attribute the scraper captures is used, and used for one job only
(full data dictionary + rationale: README.md "knowledge base" section):

  risk_ratings.<h>.risk_volatility_measures
    Alpha (Inv, Cat)      -> alpha_excess gate + score; stability across horizons
    Beta (Inv)            -> bucket-specific beta band + high-beta-needs-alpha gate
    R-Squared (Inv)       -> reliability gate: low R² means alpha/beta estimates
                             are statistically weak, so they may not be trusted
    Sharpe Ratio (Inv,Cat)-> vs-category gate + ABSOLUTE floor: Morningstar's
                             Sharpe already nets out the risk-free rate, so
                             Sharpe >= 0 == "beats risk-free risk-adjusted"
    Std Deviation(Inv,Cat)-> volatility edge score (calmer than category)
  risk_ratings.<h>.market_volatility_measures
    Upside/Downside capture -> BEST-market vs WORST-market behaviour: spread
                             scored, downside capped absolutely AND vs category
    Maximum drawdown (Inv,Cat) -> worst-case depth gate + edge score
    Drawdown dates Max Duration -> recovery speed score (time under water)
  detailed_portfolio.holdings.Equity rows
    % Portfolio Weight    -> pairwise overlap between picks (true diversification)
    Equity Star Rating    -> weight-averaged portfolio quality score
    Sector                -> effective sector count (reported; category quota
                             already enforces cross-fund diversification)
    Share Change %        -> captured; churn already summarised by turnover
  detailed_portfolio.holdings_summary
    % Assets in Top 10    -> concentration score (lower = safer)
    Reported Turnover %   -> churn score (lower suits long horizons)
    Equity/Bond/Total Holdings -> breadth, reported in metrics
  Category                -> bucket mapping (core/growth/aggressive/diversifier)
  fund name               -> Direct+Growth plan filter
  Latest NAV / NAV Date   -> staleness check (note, never a silent drop)

Design contract (mirrors mf_select.py)
=======================================
1. DETERMINISM: same (ms_data snapshot, config) -> identical run_hash.
   No RNG, no wall-clock inside the hashed payload (generated_at recorded but
   excluded), every ordering ends in a fund-name tie-break, floats r6()-rounded.
2. CAPITAL PROTECTION FIRST: hard gates run BEFORE scoring; nothing scores
   past a failed gate.
3. MEASURED, NEVER ASSUMED: em-dash cells become None; incomplete funds fail
   data_complete instead of being guessed at.
4. EXPLAINABLE: recommendation_reason strings are assembled from the same
   numbers that drove gates + score — text can never drift from arithmetic.
5. HORIZON-AWARE: the configured investment_horizon_years picks which risk
   tables lead (10y+ -> 10Y first, else 5Y first, short -> 3Y first), while
   alpha STABILITY is always judged across every horizon available.

Usage
=====
  python selection/mf_recommend.py --data ms_data --out ms_data/recommendation_run
  python selection/mf_recommend.py --selftest
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
from datetime import datetime, timezone

from mf_select import pairwise_overlap, percentile_ranks, r6, sha256_file, sha256_obj

ENGINE_VERSION = "1.2.0"

NON_HOUSE_FILES = ("morningstar_factsheet.json", "filters.json")
ALL_HORIZONS = ("3Y", "5Y", "10Y")

DEFAULT_CONFIG = {
    "engine_version": ENGINE_VERSION,
    # Drives which horizon's risk tables LEAD the gates/score. Alpha stability
    # is always cross-checked over every available horizon regardless.
    "investment_horizon_years": 10,
    # Informational: Morningstar computes Sharpe net of their risk-free proxy,
    # so gates.sharpe_min_absolute==0.0 IS the risk-free hurdle. Changing the
    # value here documents intent; raising sharpe_min_absolute enforces it.
    "risk_free_note": "Sharpe >= sharpe_min_absolute means the fund beat the "
                      "risk-free rate on a risk-adjusted basis per Morningstar's "
                      "Sharpe (already net of risk-free).",
    "universe": {
        "name_must_include": ["Direct", "Growth"],
        "name_must_exclude": ["IDCW", "Inc Dis", "Payout", "Reinvestment", "Regular"],
        "buckets": {
            "core":        ["Flexi Cap", "Large-Cap", "Large & Mid- Cap", "Focused Fund"],
            "growth":      ["Mid-Cap"],
            "aggressive":  ["Small-Cap"],
            "diversifier": ["Value", "Contra", "Dividend Yield", "Multi-Cap",
                            "Multi Asset Allocation", "Aggressive Allocation",
                            "Dynamic Asset Allocation", "Balanced Allocation"],
        },
    },
    "gates": {
        "alpha_vs_category_min_excess": 0.0,     # manager skill vs peers, must EXCEED
        "sharpe_vs_category_min_excess": 0.0,    # risk-adjusted edge vs peers
        "sharpe_min_absolute": 0.0,              # risk-free hurdle (see risk_free_note)
        "r_squared_min": 70.0,                   # below this, alpha/beta are noise
        "downside_capture_absolute_max": 110.0,  # worst-market hard cap
        "downside_capture_vs_category_tolerance": 1.05,
        "drawdown_vs_category_tolerance": 1.10,  # MDD within 1.10x category (both negative)
        # Beta discipline per bucket (README step 2): core must be defensive;
        # satellites may run hotter ONLY if alpha pays for the extra risk.
        "beta_band_by_bucket": {"core": 1.00, "growth": 1.15,
                                "aggressive": 1.15, "diversifier": 1.05},
        "high_beta_threshold": 1.00,
        "high_beta_alpha_compensation": 1.00,    # beta>1 requires alpha_excess >= this
    },
    "scoring": {
        # weights sum to 100; applied to percentile ranks of gate survivors.
        # (metric, invert) mapping lives in SCORING_FIELDS below.
        "weights": {
            "alpha_excess": 20,            # skill vs peers at the lead horizon
            "worst_alpha_excess": 15,      # alpha STABILITY: worst horizon still positive?
            "sharpe_excess": 15,           # risk-adjusted edge vs peers
            "capture_spread": 10,          # best-market gain minus worst-market pain
            "downside_capture_low": 10,    # minimise worst-market participation
            "drawdown_edge": 10,           # shallower max loss than category
            "drawdown_recovery_fast": 5,   # months under water (shorter = better)
            "std_edge": 5,                 # calmer ride than category
            "portfolio_quality": 5,        # weight-avg star rating of holdings
            "concentration_low": 3,        # top-10 % of assets (lower = safer)
            "turnover_low": 2,             # low churn suits long horizons
        },
    },
    "selection": {
        "target_count": 4,
        "bucket_quotas": {"core": 1, "growth": 1, "aggressive": 1, "diversifier": 1},
        # Structural pass order (README Step 1: the portfolio's SHAPE precedes
        # raw score — the core anchor is seated before satellites can consume
        # shared constraints like the one-fund-per-AMC slot).
        "bucket_priority": ["core", "growth", "aggressive", "diversifier"],
        "fill_remaining_from_any_bucket": True,
        "max_funds_per_amc": 1,
        "max_funds_per_category": 1,
        "max_pairwise_overlap_pct": 10.0,
    },
}

# score-weight key -> (metrics field, invert?) — invert=True means lower is better
SCORING_FIELDS = {
    "alpha_excess":           ("alpha_excess", False),
    "worst_alpha_excess":     ("worst_alpha_excess", False),
    "sharpe_excess":          ("sharpe_excess", False),
    "capture_spread":         ("capture_spread", False),
    "downside_capture_low":   ("downside_capture", True),
    "drawdown_edge":          ("drawdown_edge", False),
    "drawdown_recovery_fast": ("drawdown_duration_months", True),
    "std_edge":               ("std_edge", False),
    "portfolio_quality":      ("portfolio_quality", False),
    "concentration_low":      ("top10_pct", True),
    "turnover_low":           ("turnover_pct", True),
}


# ----------------------------------------------------------------------------
# Pure parsing helpers
# ----------------------------------------------------------------------------

def num(s):
    """Morningstar cell text -> float|None. Em-dashes and blanks are None."""
    if s is None:
        return None
    t = str(s).strip().replace(",", "")
    if t in ("", "—", "–", "-"):
        return None
    try:
        return float(t)
    except ValueError:
        return None


def fmt_signed(x):
    return f"+{x}" if x is not None and x > 0 else f"{x}"


def parse_duration_months(s):
    """'5 Months' -> 5.0, '1 Year 2 Months' -> 14.0, '2 Years' -> 24.0."""
    if not s:
        return None
    t = str(s).lower()
    total, found = 0.0, False
    m = re.search(r"(\d+)\s*year", t)
    if m:
        total += int(m.group(1)) * 12
        found = True
    m = re.search(r"(\d+)\s*month", t)
    if m:
        total += int(m.group(1))
        found = True
    return total if found else None


def parse_nav_date(s):
    """'Jul 02, 2026' -> '2026-07-02' (ISO) or None."""
    try:
        return datetime.strptime(str(s).strip(), "%b %d, %Y").date().isoformat()
    except (ValueError, TypeError):
        return None


def derive_horizon_preference(years):
    """Investment horizon -> which risk tables LEAD. Long money should be
    judged on long windows first; the shorter windows remain the fallback
    because 10Y tables are often missing for younger funds."""
    if years >= 10:
        return ["10Y", "5Y", "3Y"]
    if years >= 5:
        return ["5Y", "3Y"]
    return ["3Y", "5Y"]


def name_passes_plan_filter(name, cfg_u):
    low = name.lower()
    if any(tok.lower() not in low for tok in cfg_u["name_must_include"]):
        return False
    if any(tok.lower() in low for tok in cfg_u["name_must_exclude"]):
        return False
    return True


def bucket_for(category, buckets):
    for bucket, cats in sorted(buckets.items()):
        if category in cats:
            return bucket
    return None


def horizon_metrics(fund, horizon):
    """Extract one horizon's numeric metrics from an enriched fund entry.
    Returns None when that horizon has no risk table at all."""
    rr = (fund.get("risk_ratings") or {}).get(horizon)
    if not rr:
        return None
    rv = rr.get("risk_volatility_measures") or {}
    mv = rr.get("market_volatility_measures") or {}

    def pair(metric):
        row = rv.get(metric) or {}
        return num(row.get("Investment")), num(row.get("Category"))

    alpha, alpha_cat = pair("Alpha")
    beta, _ = pair("Beta")
    sharpe, sharpe_cat = pair("Sharpe Ratio")
    std, std_cat = pair("Standard Deviation")
    r2, _ = pair("R-Squared")
    cap = mv.get("capture_ratios") or {}
    up = num((cap.get("Upside") or {}).get("Investment"))
    dn = num((cap.get("Downside") or {}).get("Investment"))
    dn_cat = num((cap.get("Downside") or {}).get("Category"))
    dd = (mv.get("drawdown") or {}).get("Maximum") or {}
    mdd, mdd_cat = num(dd.get("Investment %")), num(dd.get("Category %"))
    duration = parse_duration_months(
        (mv.get("drawdown_dates") or {}).get("Max Duration"))

    return {
        "horizon": horizon,
        "alpha": r6(alpha), "alpha_cat": r6(alpha_cat),
        "alpha_excess": r6(alpha - alpha_cat) if None not in (alpha, alpha_cat) else None,
        "beta": r6(beta),
        "sharpe": r6(sharpe), "sharpe_cat": r6(sharpe_cat),
        "sharpe_excess": r6(sharpe - sharpe_cat) if None not in (sharpe, sharpe_cat) else None,
        "std": r6(std), "std_cat": r6(std_cat),
        "std_edge": r6(std_cat - std) if None not in (std, std_cat) else None,
        "r_squared": r6(r2),
        "upside_capture": r6(up), "downside_capture": r6(dn),
        "downside_capture_cat": r6(dn_cat),
        "capture_spread": r6(up - dn) if None not in (up, dn) else None,
        "max_drawdown": r6(mdd), "max_drawdown_cat": r6(mdd_cat),
        "drawdown_edge": r6(mdd - mdd_cat) if None not in (mdd, mdd_cat) else None,
        "drawdown_duration_months": r6(duration),
    }


# Every input the gates need. A horizon only counts as "complete" when ALL of
# these are present, so every gate is evaluable at one coherent horizon.
REQUIRED_FOR_GATES = (
    "alpha_excess", "sharpe_excess", "sharpe", "beta", "r_squared",
    "downside_capture", "downside_capture_cat",
    "max_drawdown", "max_drawdown_cat",
)


def pick_horizon(fund, preference):
    """Nearest-complete-horizon rule: use the first preferred horizon where
    EVERY gate input is present. A missing category cell at the lead horizon
    (e.g. Morningstar publishes no 10Y category drawdown for some funds) no
    longer disqualifies a fund whose 5Y tables are complete — it is simply
    evaluated at 5Y. If no horizon is complete, the first that exists is
    returned marked incomplete and fails the data_complete gate (values are
    never guessed, and gates are never skipped piecemeal)."""
    first_existing = None
    for h in preference:
        m = horizon_metrics(fund, h)
        if m is None:
            continue
        if first_existing is None:
            first_existing = m
        if all(m[k] is not None for k in REQUIRED_FOR_GATES):
            m["complete"] = True
            return m
    if first_existing is not None:
        first_existing["complete"] = False
    return first_existing


def cross_horizon_stability(fund):
    """Alpha/capture behaviour across EVERY available horizon — a fund whose
    alpha only exists in one lucky window is not the same as one that beat
    its category in 3Y, 5Y and 10Y. Returns (worst_alpha_excess,
    n_horizons_alpha_positive, n_horizons_with_alpha, n_horizons_capture_positive)."""
    excesses, spreads = [], []
    for h in ALL_HORIZONS:
        m = horizon_metrics(fund, h)
        if not m:
            continue
        if m["alpha_excess"] is not None:
            excesses.append(m["alpha_excess"])
        if m["capture_spread"] is not None:
            spreads.append(m["capture_spread"])
    worst = r6(min(excesses)) if excesses else None
    return (worst,
            sum(1 for e in excesses if e > 0), len(excesses),
            sum(1 for s in spreads if s > 0))


def equity_weights(fund):
    """{holding name: weight} from scraped equity holdings; '—' weights and
    duplicate names handled deterministically (first occurrence wins)."""
    rows = ((fund.get("detailed_portfolio") or {}).get("holdings") or {}).get("Equity")
    if not isinstance(rows, list):
        return {}
    w = {}
    for row in rows:
        v = num(row.get("% Portfolio Weight"))
        name = (row.get("Holdings") or "").strip()
        if name and v is not None and name not in w:
            w[name] = v
    return w


def portfolio_quality(fund):
    """Weight-averaged Equity Star Rating of the scraped holdings — a proxy
    for the quality of what the manager actually owns."""
    rows = ((fund.get("detailed_portfolio") or {}).get("holdings") or {}).get("Equity")
    if not isinstance(rows, list):
        return None
    acc = tot = 0.0
    for row in rows:
        star, w = num(row.get("Equity Star Rating")), num(row.get("% Portfolio Weight"))
        if star is not None and w is not None and w > 0:
            acc += star * w
            tot += w
    return r6(acc / tot) if tot > 0 else None


def sector_effective_n(fund):
    """Effective number of sectors (inverse Herfindahl of sector weights):
    10 holdings all in one sector -> 1.0; evenly spread over 5 sectors -> 5.0."""
    rows = ((fund.get("detailed_portfolio") or {}).get("holdings") or {}).get("Equity")
    if not isinstance(rows, list):
        return None
    by_sector = {}
    for row in rows:
        w, sec = num(row.get("% Portfolio Weight")), (row.get("Sector") or "").strip()
        if sec and w is not None and w > 0:
            by_sector[sec] = by_sector.get(sec, 0.0) + w
    tot = sum(by_sector.values())
    if tot <= 0:
        return None
    hhi = sum((w / tot) ** 2 for w in by_sector.values())
    return r6(1.0 / hhi) if hhi > 0 else None


def build_reason(m, bucket, category):
    """Deterministic recommendation reason assembled from the SAME numbers
    that drive gates + score, so text and arithmetic can never diverge."""
    h = m["horizon_used"]
    parts = [f"{bucket or 'unbucketed'} pick ({category})"]
    if m["alpha_excess"] is not None:
        parts.append(f"{h} alpha {m['alpha']} vs category {m['alpha_cat']} "
                     f"({fmt_signed(m['alpha_excess'])} excess)")
    if m["worst_alpha_excess"] is not None:
        parts.append(f"alpha stability: worst-horizon excess "
                     f"{fmt_signed(m['worst_alpha_excess'])} and positive in "
                     f"{m['alpha_consistency']}/{m['alpha_horizons']} horizons")
    if m["sharpe_excess"] is not None:
        hurdle = " (clears the risk-free hurdle)" if (m["sharpe"] or 0) > 0 else ""
        parts.append(f"Sharpe {m['sharpe']} vs {m['sharpe_cat']} "
                     f"({fmt_signed(m['sharpe_excess'])}){hurdle}")
    if m["beta"] is not None:
        parts.append(f"beta {m['beta']}")
    if m["upside_capture"] is not None and m["downside_capture"] is not None:
        parts.append(f"captures {m['upside_capture']} of up-markets vs "
                     f"{m['downside_capture']} of down-markets "
                     f"(spread {fmt_signed(m['capture_spread'])})")
    if m["drawdown_edge"] is not None:
        rel = "shallower" if m["drawdown_edge"] > 0 else "deeper"
        parts.append(f"max drawdown {m['max_drawdown']}% vs category "
                     f"{m['max_drawdown_cat']}% ({rel})")
    if m["drawdown_duration_months"] is not None:
        parts.append(f"recovered from max drawdown in "
                     f"{m['drawdown_duration_months']:g} months")
    quality = []
    if m["portfolio_quality"] is not None:
        quality.append(f"holdings avg star {m['portfolio_quality']}")
    if m["top10_pct"] is not None:
        quality.append(f"top-10 = {m['top10_pct']:g}% of assets")
    if m["turnover_pct"] is not None:
        quality.append(f"turnover {m['turnover_pct']:g}%")
    if quality:
        parts.append(", ".join(quality))
    return "; ".join(parts)


# ----------------------------------------------------------------------------
# Engine
# ----------------------------------------------------------------------------

class RecommendationEngine:
    def __init__(self, data_dir, config):
        self.data_dir = data_dir
        self.cfg = config
        self.notes = []
        self.input_hashes = {}

    # ---------------- load ----------------
    def load(self):
        self.funds = {}
        paths = sorted(glob.glob(os.path.join(self.data_dir, "*.json")))
        for p in paths:
            base = os.path.basename(p)
            if base in NON_HOUSE_FILES or base.startswith("fund_urls_"):
                continue
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                continue
            enriched = {n: v for n, v in data.items()
                        if isinstance(v, dict) and "risk_ratings" in v}
            if not enriched:
                continue
            self.input_hashes[base] = sha256_file(p)
            house = base[:-5]
            for name, entry in sorted(enriched.items()):
                self.funds[name] = {"house": house,
                                    "category": entry.get("Category", ""),
                                    "raw": entry}
        if not self.funds:
            raise SystemExit(
                f"No enriched funds found under {self.data_dir}/ — run the "
                "scraper pipeline (morningstar_factsheet.py then "
                "morningstar_fund_details.py) first.")

        u = self.cfg["universe"]
        self.universe = []
        for name in sorted(self.funds):
            info = self.funds[name]
            if not name_passes_plan_filter(name, u):
                continue
            bucket = bucket_for(info["category"], u["buckets"])
            if bucket is None:
                continue
            self.universe.append({"name": name, "bucket": bucket, **info})
        self.notes.append(
            f"{len(self.funds)} enriched funds loaded; {len(self.universe)} in "
            "universe after plan filter (Direct+Growth) and bucket mapping.")

    # ---------------- metrics ----------------
    def compute_metrics(self):
        pref = (self.cfg.get("horizon_preference")
                or derive_horizon_preference(self.cfg["investment_horizon_years"]))
        self.horizon_preference = pref
        self.metrics = {}
        nav_dates = {}
        for f in self.universe:
            raw = f["raw"]
            m = pick_horizon(raw, pref)
            worst, n_pos, n_alpha, n_capture = cross_horizon_stability(raw)
            summary = (raw.get("detailed_portfolio") or {}).get("holdings_summary") or {}
            nav_date = parse_nav_date(raw.get("NAV Date"))
            nav_dates[f["name"]] = nav_date
            self.metrics[f["name"]] = {
                "horizon_used": m["horizon"] if m else None,
                "data_complete": bool(m and m.get("complete")),
                "worst_alpha_excess": worst,
                "alpha_consistency": n_pos,
                "alpha_horizons": n_alpha,
                "capture_consistency": n_capture,
                "portfolio_quality": portfolio_quality(raw),
                "sector_effective_n": sector_effective_n(raw),
                "top10_pct": num(summary.get("% Assets in Top 10 Holdings")),
                "turnover_pct": num(summary.get("Reported Turnover %")),
                "total_holdings": num(summary.get("Total Holdings")),
                "nav_date": nav_date,
                **{k: (m or {}).get(k) for k in (
                    "alpha", "alpha_cat", "alpha_excess", "beta", "sharpe",
                    "sharpe_cat", "sharpe_excess", "std", "std_cat", "std_edge",
                    "r_squared", "upside_capture", "downside_capture",
                    "downside_capture_cat", "capture_spread", "max_drawdown",
                    "max_drawdown_cat", "drawdown_edge",
                    "drawdown_duration_months")},
            }
        known = sorted(d for d in nav_dates.values() if d)
        if known:
            latest = known[-1]
            stale = sorted(n for n, d in nav_dates.items() if d and d < latest)
            if stale:
                self.notes.append(
                    f"{len(stale)} fund(s) have NAV dates older than the "
                    f"snapshot max ({latest}) — pricing may be stale: "
                    f"{stale[:5]}{'…' if len(stale) > 5 else ''}")
        self.notes.append(
            f"horizon preference {pref} derived from "
            f"investment_horizon_years={self.cfg['investment_horizon_years']}.")

    # ---------------- gates ----------------
    def apply_gates(self):
        g = self.cfg["gates"]
        self.gate_results = {}
        for f in self.universe:
            m = self.metrics[f["name"]]
            checks = {"data_complete": m["data_complete"]}
            checks["alpha_vs_category"] = (
                m["alpha_excess"] is not None
                and m["alpha_excess"] > g["alpha_vs_category_min_excess"])
            checks["sharpe_vs_category"] = (
                m["sharpe_excess"] is not None
                and m["sharpe_excess"] >= g["sharpe_vs_category_min_excess"])
            checks["sharpe_beats_risk_free"] = (
                m["sharpe"] is not None
                and m["sharpe"] >= g["sharpe_min_absolute"])
            checks["r_squared_reliability"] = (
                m["r_squared"] is not None
                and m["r_squared"] >= g["r_squared_min"])
            band = g["beta_band_by_bucket"].get(f["bucket"], 1.15)
            checks["beta_within_bucket_band"] = (
                m["beta"] is not None and m["beta"] <= band)
            checks["high_beta_alpha_compensation"] = (
                m["beta"] is None
                or m["beta"] <= g["high_beta_threshold"]
                or (m["alpha_excess"] is not None
                    and m["alpha_excess"] >= g["high_beta_alpha_compensation"]))
            checks["downside_capture_cap"] = (
                m["downside_capture"] is not None
                and m["downside_capture"] <= g["downside_capture_absolute_max"])
            checks["downside_capture_vs_category"] = (
                m["downside_capture"] is not None
                and m["downside_capture_cat"] is not None
                and m["downside_capture"]
                <= m["downside_capture_cat"] * g["downside_capture_vs_category_tolerance"])
            checks["drawdown_vs_category"] = (
                m["max_drawdown"] is not None and m["max_drawdown_cat"] is not None
                and m["max_drawdown"]
                >= m["max_drawdown_cat"] * g["drawdown_vs_category_tolerance"])
            self.gate_results[f["name"]] = {
                "checks": checks, "passed": all(checks.values())}

    # ---------------- scoring ----------------
    def score(self):
        w = self.cfg["scoring"]["weights"]
        self.survivors = [f for f in self.universe
                          if self.gate_results[f["name"]]["passed"]]
        names = [f["name"] for f in self.survivors]

        def col(metric, invert):
            vals = []
            for n in names:
                v = self.metrics[n].get(metric)
                vals.append((n, -v if (invert and v is not None) else v))
            return percentile_ranks(vals)

        cols = {key: col(*SCORING_FIELDS[key]) for key in w}
        total = sum(w.values())
        self.scores = {}
        for n in names:
            self.scores[n] = r6(sum(w[k] * cols[k][n] for k in w) / total)

    # ---------------- selection ----------------
    def _rank_key(self, f):
        n = f["name"]
        m = self.metrics[n]
        return (-(self.scores.get(n, 0.0)),
                -(m.get("alpha_excess") or -999),
                -(m.get("sharpe_excess") or -999),
                n)                                   # absolute final tie-break

    def select(self):
        sel = self.cfg["selection"]
        ranked = sorted(self.survivors, key=self._rank_key)
        quotas = dict(sel["bucket_quotas"])
        priority = sel.get("bucket_priority") or sorted(quotas)
        picked, picked_names, decisions = [], set(), []
        weights = {f["name"]: equity_weights(f["raw"]) for f in ranked}

        def blocked_by(f):
            """AMC / category / overlap constraints vs funds already picked."""
            if sel["max_funds_per_amc"] and sum(
                    1 for p in picked if p["house"] == f["house"]) >= sel["max_funds_per_amc"]:
                return "amc_limit"
            if sel["max_funds_per_category"] and sum(
                    1 for p in picked if p["category"] == f["category"]) >= sel["max_funds_per_category"]:
                return "category_limit"
            wa = weights.get(f["name"]) or {}
            for p in picked:
                wb = weights.get(p["name"]) or {}
                if wa and wb:
                    ov = pairwise_overlap(wa, wb)
                    if ov > sel["max_pairwise_overlap_pct"]:
                        return f"overlap_{ov}pct_with_{p['name']}"
            return None

        # PASS 1 — STRUCTURE: seat each bucket in priority order (core first)
        # so a high-scoring satellite can no longer consume a shared constraint
        # (e.g. the one-per-AMC slot) that the core anchor needed. Within a
        # bucket the best-scored eligible fund wins.
        for bucket in priority:
            for _ in range(max(0, quotas.get(bucket, 0))):
                if len(picked) >= sel["target_count"]:
                    break
                for f in ranked:
                    if f["name"] in picked_names or f["bucket"] != bucket:
                        continue
                    why = blocked_by(f)
                    if why:
                        decisions.append({"fund": f["name"], "action": "skipped",
                                          "reason": why,
                                          "pass": f"structure:{bucket}"})
                        continue
                    picked.append(f)
                    picked_names.add(f["name"])
                    decisions.append({"fund": f["name"], "action": "selected",
                                      "bucket": bucket, "pass": "structure"})
                    break

        # PASS 2 — FILL: best remaining by score, any bucket, same constraints.
        if sel["fill_remaining_from_any_bucket"]:
            for f in ranked:
                if len(picked) >= sel["target_count"]:
                    break
                if f["name"] in picked_names:
                    continue
                why = blocked_by(f)
                if why:
                    decisions.append({"fund": f["name"], "action": "skipped",
                                      "reason": why, "pass": "fill"})
                    continue
                picked.append(f)
                picked_names.add(f["name"])
                decisions.append({"fund": f["name"], "action": "selected",
                                  "bucket": f["bucket"], "pass": "fill"})
        self.selection, self.decisions, self.ranked = picked, decisions, ranked
        self.holdings_weights = weights

    # ---------------- report ----------------
    def run(self):
        self.load()
        self.compute_metrics()
        self.apply_gates()
        self.score()
        self.select()
        return self.report()

    def report(self):
        overlap_matrix = {}
        for i, a in enumerate(self.selection):
            for b in self.selection[i + 1:]:
                wa = self.holdings_weights.get(a["name"]) or {}
                wb = self.holdings_weights.get(b["name"]) or {}
                if wa and wb:
                    overlap_matrix[f"{a['name']} x {b['name']}"] = pairwise_overlap(wa, wb)

        rep = {
            "engine_version": ENGINE_VERSION,
            "horizon_preference": self.horizon_preference,
            "config_hash": sha256_obj(self.cfg),
            "input_hashes": self.input_hashes,
            "universe_size": len(self.universe),
            "gates_passed": sum(1 for g in self.gate_results.values() if g["passed"]),
            "ranking": [
                {"rank": i + 1, "fund": f["name"], "fund_house": f["house"],
                 "category": f["category"], "bucket": f["bucket"],
                 "score": self.scores.get(f["name"]),
                 "metrics": self.metrics[f["name"]],
                 "gates": self.gate_results[f["name"]]}
                for i, f in enumerate(self.ranked)
            ],
            "excluded_by_gates": [
                {"fund": f["name"], "category": f["category"],
                 "failed_checks": sorted(
                     k for k, ok in self.gate_results[f["name"]]["checks"].items() if not ok)}
                for f in self.universe
                if not self.gate_results[f["name"]]["passed"]
            ],
            "recommendations": [
                {"rank": i + 1, "fund": f["name"], "fund_house": f["house"],
                 "category": f["category"], "bucket": f["bucket"],
                 "score": self.scores.get(f["name"]),
                 "recommendation_reason": build_reason(
                     self.metrics[f["name"]], f["bucket"], f["category"])}
                for i, f in enumerate(self.selection)
            ],
            "selection_decisions": self.decisions,
            "overlap_matrix_pct": overlap_matrix,
            "notes_and_caveats": self.notes + [
                "Metrics are Morningstar's published risk tables for the scraped "
                "snapshot; they change as the site updates — the input_hashes pin "
                "exactly which snapshot produced this report.",
                "Overlap uses scraped equity-holding NAMES (Morningstar's public "
                "table may list fewer rows than the fund's full portfolio), so "
                "treat it as a lower bound on true overlap.",
                "Past performance does not predict future returns; mutual fund "
                "investments are subject to market risk.",
                "Determinism guarantee: identical config_hash + input_hashes -> "
                "identical run_hash. If a re-run differs, diff the hashes first.",
            ],
        }
        rep["run_hash"] = sha256_obj(rep)          # excludes generated_at below
        rep["generated_at"] = datetime.now(timezone.utc).isoformat()
        return rep


def write_report(rep, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    jpath = os.path.join(out_dir, "recommendations.json")
    with open(jpath, "w", encoding="utf-8") as f:
        json.dump(rep, f, indent=2, sort_keys=True, ensure_ascii=False)
    lines = [
        "# Mutual Fund Recommendations",
        "",
        f"- engine: v{rep['engine_version']}  |  run_hash: `{rep['run_hash'][:16]}…`",
        f"- horizons: {' > '.join(rep['horizon_preference'])}"
        f"  |  universe: {rep['universe_size']}  |  passed gates: {rep['gates_passed']}"
        f"  |  recommended: {len(rep['recommendations'])}",
        "",
    ]
    for r in rep["recommendations"]:
        lines += [f"## {r['rank']}. {r['fund']}  (score {r['score']})",
                  f"*{r['fund_house']} — {r['category']} — {r['bucket']} bucket*",
                  "", r["recommendation_reason"], ""]
    if rep["overlap_matrix_pct"]:
        lines.append("### Pairwise equity overlap (%)")
        for k, v in sorted(rep["overlap_matrix_pct"].items()):
            lines.append(f"- {k}: {v}")
        lines.append("")
    lines.append("### Caveats")
    lines += [f"- {n}" for n in rep["notes_and_caveats"]]
    with open(os.path.join(out_dir, "recommendations.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return jpath


# ----------------------------------------------------------------------------
# Selftest: synthetic enriched snapshot, no browser, no RNG
# ----------------------------------------------------------------------------

def _synthetic_fund(category, alpha, alpha_cat, sharpe, sharpe_cat, beta,
                    up, dn, dn_cat, mdd, mdd_cat, holdings, turnover="45.00"):
    def rv():
        return {"Alpha": {"Investment": str(alpha), "Category": str(alpha_cat), "Index": "–"},
                "Beta": {"Investment": str(beta), "Category": "1.00", "Index": "–"},
                "R-Squared": {"Investment": "92.00", "Category": "90.00", "Index": "–"},
                "Sharpe Ratio": {"Investment": str(sharpe), "Category": str(sharpe_cat), "Index": "–"},
                "Standard Deviation": {"Investment": "12.00", "Category": "13.00", "Index": "–"}}
    def mv():
        return {"capture_ratios": {
                    "Upside": {"Investment": str(up), "Category": "100", "Index": "—"},
                    "Downside": {"Investment": str(dn), "Category": str(dn_cat), "Index": "—"}},
                "drawdown": {"Maximum": {"Investment %": str(mdd),
                                         "Category %": str(mdd_cat), "Index %": "—"}},
                "drawdown_dates": {"Peak": "10/01/2024", "Valley": "02/28/2025",
                                   "Max Duration": "5 Months"}}
    def year():   # fresh dicts per horizon so tests can mutate one horizon
        return {"risk_volatility_measures": rv(), "market_volatility_measures": mv()}
    sectors = ["Financial Services", "Technology", "Energy", "Industrials"]
    return {
        "Action": "Factsheet", "Category": category,
        "Latest NAV": "100.0", "NAV Date": "Jul 02, 2026",
        "risk_ratings": {"3Y": year(), "5Y": year(), "10Y": year()},
        "detailed_portfolio": {
            "holdings_summary": {"Equity Holdings": str(len(holdings)),
                                 "Total Holdings": str(len(holdings)),
                                 "% Assets in Top 10 Holdings": "40",
                                 "Reported Turnover %": turnover},
            "holdings": {"Equity": [
                {"Holdings": h, "% Portfolio Weight": str(w), "Share Change %": "0.00",
                 "Equity Star Rating": "3", "Sector": sectors[i % len(sectors)]}
                for i, (h, w) in enumerate(holdings.items())], "Bond": []}},
    }


def selftest():
    import shutil
    import tempfile
    tmp = tempfile.mkdtemp()
    try:
        houses = {
            "AMC_Alpha": {
                "Core Star Fund Direct Growth": _synthetic_fund(
                    "Flexi Cap", 2.5, 1.0, 0.9, 0.6, 0.90, 105, 90, 100,
                    -10.0, -13.0, {"Stock A": 40.0, "Stock B": 35.0, "Stock C": 25.0}),
                # 65% overlap twin of the core pick — must never co-exist with it
                "Core Twin Fund Direct Growth": _synthetic_fund(
                    "Large-Cap", 2.0, 1.0, 0.8, 0.6, 0.92, 104, 92, 100,
                    -10.5, -13.0, {"Stock A": 35.0, "Stock B": 30.0, "Stock X": 35.0}),
                # HIGHEST score in the whole universe but a diversifier sharing
                # the core anchor's AMC — bucket-priority selection must seat
                # the core fund first and route the diversifier slot elsewhere.
                "Star Diversifier Fund Direct Growth": _synthetic_fund(
                    "Multi Asset Allocation", 5.0, 1.0, 1.2, 0.6, 0.85, 120, 60, 100,
                    -8.0, -12.0, {"Stock P": 50.0, "Stock Q": 50.0}),
            },
            "AMC_Beta": {
                "Mid Momentum Fund Direct Growth": _synthetic_fund(
                    "Mid-Cap", 3.0, 1.5, 1.0, 0.7, 1.05, 110, 95, 105,
                    -14.0, -17.0, {"Stock D": 50.0, "Stock E": 50.0}),
                # weak fund: negative alpha excess -> must fail the alpha gate
                "Weak Laggard Fund Direct Growth": _synthetic_fund(
                    "Mid-Cap", 0.5, 1.5, 0.5, 0.7, 1.00, 98, 104, 100,
                    -20.0, -17.0, {"Stock F": 100.0}),
                # hot beta without the alpha to pay for it -> compensation gate
                "Beta Heavy Fund Direct Growth": _synthetic_fund(
                    "Mid-Cap", 1.8, 1.5, 0.75, 0.7, 1.10, 108, 100, 105,
                    -16.0, -17.0, {"Stock K": 60.0, "Stock L": 40.0}),
            },
            "AMC_Gamma": {
                "Small Rocket Fund Direct Growth": _synthetic_fund(
                    "Small-Cap", 4.0, 2.0, 1.1, 0.8, 1.10, 118, 96, 108,
                    -18.0, -22.0, {"Stock G": 60.0, "Stock H": 40.0}),
                # plan filter: Regular plan must never enter the universe
                "Small Rocket Fund Regular Growth": _synthetic_fund(
                    "Small-Cap", 4.0, 2.0, 1.1, 0.8, 1.10, 118, 96, 108,
                    -18.0, -22.0, {"Stock G": 60.0, "Stock H": 40.0}),
            },
            "AMC_Delta": {
                "Value Anchor Fund Direct Growth": _synthetic_fund(
                    "Value", 1.8, 0.9, 0.85, 0.55, 0.88, 103, 89, 99,
                    -9.0, -12.0, {"Stock I": 70.0, "Stock J": 30.0}),
            },
        }
        for house, funds in houses.items():
            with open(os.path.join(tmp, f"{house}.json"), "w", encoding="utf-8") as f:
                json.dump(funds, f, indent=2, sort_keys=True)

        cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        assert sum(cfg["scoring"]["weights"].values()) == 100, "weights must sum to 100"
        rep1 = RecommendationEngine(tmp, cfg).run()
        rep2 = RecommendationEngine(tmp, cfg).run()

        assert rep1["run_hash"] == rep2["run_hash"], "DETERMINISM FAILED"
        picks = [r["fund"] for r in rep1["recommendations"]]
        excluded = {e["fund"]: e["failed_checks"] for e in rep1["excluded_by_gates"]}
        assert "Weak Laggard Fund Direct Growth" in excluded, "weak fund not gated"
        assert "alpha_vs_category" in excluded["Weak Laggard Fund Direct Growth"]
        assert "Beta Heavy Fund Direct Growth" in excluded, "hot-beta fund not gated"
        assert "high_beta_alpha_compensation" in excluded["Beta Heavy Fund Direct Growth"]
        assert not ("Core Star Fund Direct Growth" in picks
                    and "Core Twin Fund Direct Growth" in picks), "overlap pair co-selected"
        assert all("Regular" not in p for p in picks), "plan filter leaked Regular"
        assert len(picks) == 4, f"expected 4 picks, got {picks}"
        # bucket-priority: the core anchor is seated even though a same-AMC
        # diversifier out-scores it; the diversifier slot goes elsewhere
        assert "Core Star Fund Direct Growth" in picks, "core anchor not seated"
        assert "Star Diversifier Fund Direct Growth" not in picks, \
            "same-AMC diversifier displaced the core anchor"
        assert picks[0] == "Core Star Fund Direct Growth", "core must be seated first"
        # horizon fallback: missing 10Y category drawdown -> evaluate at 5Y
        fb = _synthetic_fund("Flexi Cap", 2.5, 1.0, 0.9, 0.6, 0.90,
                             105, 90, 100, -10.0, -13.0, {"Stock Z": 100.0})
        fb["risk_ratings"]["10Y"]["market_volatility_measures"]["drawdown"][
            "Maximum"]["Category %"] = "—"
        m = pick_horizon(fb, ["10Y", "5Y", "3Y"])
        assert m["horizon"] == "5Y" and m["complete"], \
            "incomplete 10Y must fall back to complete 5Y"
        for v in rep1["overlap_matrix_pct"].values():
            assert v <= cfg["selection"]["max_pairwise_overlap_pct"]
        for r in rep1["recommendations"]:
            for token in ("alpha", "up-markets", "recovered", "avg star"):
                assert token in r["recommendation_reason"], f"reason missing {token!r}"
        cfg2 = json.loads(json.dumps(cfg))
        cfg2["gates"]["r_squared_min"] = 80.0
        rep3 = RecommendationEngine(tmp, cfg2).run()
        assert rep3["config_hash"] != rep1["config_hash"]
        # horizon derivation: short horizon must lead with 3Y tables
        cfg3 = json.loads(json.dumps(cfg))
        cfg3["investment_horizon_years"] = 3
        rep4 = RecommendationEngine(tmp, cfg3).run()
        assert rep4["horizon_preference"][0] == "3Y"

        print("SELFTEST PASS")
        print(f"  universe={rep1['universe_size']} gates_passed={rep1['gates_passed']}")
        print(f"  picks={picks}")
        print(f"  run_hash={rep1['run_hash'][:16]}… (identical across both runs)")
        return 0
    finally:
        shutil.rmtree(tmp)


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Deterministic MF recommendation engine (Morningstar snapshot)")
    ap.add_argument("--data", default="ms_data",
                    help="dir of enriched per-house JSON files")
    ap.add_argument("--config", help="JSON overrides merged onto DEFAULT_CONFIG")
    ap.add_argument("--out", default="ms_data/recommendation_run")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        sys.exit(selftest())

    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if args.config:
        with open(args.config, encoding="utf-8") as f:
            user_cfg = json.load(f)

        def merge(base, over):
            for k, v in over.items():
                if isinstance(v, dict) and isinstance(base.get(k), dict):
                    merge(base[k], v)
                else:
                    base[k] = v
        merge(cfg, user_cfg)

    rep = RecommendationEngine(args.data, cfg).run()
    jpath = write_report(rep, args.out)
    print(f"universe={rep['universe_size']} gates_passed={rep['gates_passed']} "
          f"recommended={len(rep['recommendations'])}")
    for r in rep["recommendations"]:
        print(f"  {r['rank']}. {r['fund']} (score {r['score']}, {r['bucket']})")
    print(f"run_hash={rep['run_hash']}")
    print(f"report -> {jpath}")


if __name__ == "__main__":
    main()
