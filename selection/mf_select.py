#!/usr/bin/env python3
"""
mf_select.py — Deterministic long-horizon (>10Y) Direct Mutual Fund selection framework.

Design contract
===============
1. DETERMINISM: same (dataset snapshot, benchmark files, holdings files, config)
   -> byte-identical ranking and selection. No randomness, no wall-clock dependence
   (the as-of date is pinned in config or derived from the data itself), and every
   comparison uses a total ordering with scheme_code as the final tie-break.
   Each run emits a manifest with SHA-256 hashes of config + inputs so two runs
   can be proven identical (or proven to differ because the *data* changed).

2. CAPITAL PROTECTION FIRST: hard gates eliminate funds before any scoring.
   A fund cannot "score its way past" a failed gate.

3. ALPHA IS MEASURED, NEVER ASSUMED:
   - alpha vs INDEX requires a user-supplied benchmark TRI series (CSV).
     NAV data does not contain it and the framework will not fabricate it.
   - alpha vs CATEGORY is computed against an equal-weighted category composite
     built from the same dataset (deterministic, auditable, but survivorship-
     biased — flagged in output).

4. OVERLAP is computed from user-supplied holdings CSVs (ISIN, weight_pct),
   pairwise overlap = sum(min(wA_i, wB_i)) over common ISINs. Without holdings
   files, overlap is UNKNOWN and the framework falls back to a category-exclusion
   proxy and says so — it never silently claims "<10% overlap".

Inputs
======
  --dataset   mf_dataset/ folder produced by indian-mf-data's build_mf_dataset.py
  --config    framework_config.json (rules; hashed into the manifest)
  --benchmarks dir of <benchmark_key>.csv files: date,value (TRI preferred)
  --holdings  dir of <scheme_code>.csv files: isin,weight_pct
  --out       output dir for report.json / report.md / manifest.json

Usage
=====
  python mf_select.py --dataset mf_dataset --config framework_config.json \
      --benchmarks benchmarks/ --holdings holdings/ --out run_2026_07
  python mf_select.py --selftest
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import sys
from datetime import date, timedelta

FRAMEWORK_VERSION = "1.0.0"

# ----------------------------------------------------------------------------
# Deterministic helpers
# ----------------------------------------------------------------------------

def r6(x):
    """Round to 6 dp before any comparison/serialisation so float noise can
    never flip an ordering between runs/platforms."""
    return None if x is None else round(float(x), 6)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_obj(obj):
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def parse_date(s):
    y, m, d = s.split("-")
    return date(int(y), int(m), int(d))


# ----------------------------------------------------------------------------
# Series utilities (a series is a list of (date, float) ascending)
# ----------------------------------------------------------------------------

def load_history(path):
    with open(path, "r", encoding="utf-8") as f:
        doc = json.load(f)
    series = [(parse_date(p["date"]), float(p["nav"])) for p in doc["nav"]]
    series.sort(key=lambda t: t[0])
    return doc.get("meta", {}), series


def load_benchmark_csv(path):
    series = []
    with open(path, "r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            series.append((parse_date(row["date"].strip()), float(row["value"])))
    series.sort(key=lambda t: t[0])
    return series


def clip(series, as_of):
    return [p for p in series if p[0] <= as_of]


def value_on_or_before(series, d):
    """Last value at or before d (binary search not needed; linear from end is
    fine for clarity — series are clipped once per fund)."""
    for pd_, v in reversed(series):
        if pd_ <= d:
            return v
    return None


def cagr_between(series, start_d, end_d):
    v0 = value_on_or_before(series, start_d)
    v1 = value_on_or_before(series, end_d)
    if v0 is None or v1 is None or v0 <= 0:
        return None
    years = (end_d - start_d).days / 365.25
    if years <= 0:
        return None
    return (v1 / v0) ** (1.0 / years) - 1.0


def rolling_cagrs(series, window_years, step_days=30):
    """Deterministic rolling windows: end dates step backward from the series
    end in fixed step_days; window start = end - window_years*365.25 days."""
    if not series:
        return []
    first_d, last_d = series[0][0], series[-1][0]
    win = timedelta(days=int(window_years * 365.25))
    out = []
    end = last_d
    while end - win >= first_d:
        c = cagr_between(series, end - win, end)
        if c is not None:
            out.append(c)
        end = end - timedelta(days=step_days)
    return out  # order: most recent window first (deterministic)


def daily_returns(series):
    rets = []
    for i in range(1, len(series)):
        p0, p1 = series[i - 1][1], series[i][1]
        if p0 > 0:
            rets.append((series[i][0], p1 / p0 - 1.0))
    return rets


def annualised_vol(series):
    rets = [r for _, r in daily_returns(series)]
    n = len(rets)
    if n < 30:
        return None
    mu = sum(rets) / n
    var = sum((r - mu) ** 2 for r in rets) / (n - 1)
    return math.sqrt(var) * math.sqrt(252)


def sortino(series, rf, years):
    """Sortino over the trailing `years`: (CAGR - rf) / downside deviation."""
    end_d = series[-1][0]
    start_d = end_d - timedelta(days=int(years * 365.25))
    window = [p for p in series if p[0] >= start_d]
    if len(window) < 100:
        return None
    c = cagr_between(series, start_d, end_d)
    if c is None:
        return None
    daily_rf = rf / 252.0
    downs = [min(0.0, r - daily_rf) for _, r in daily_returns(window)]
    dd = math.sqrt(sum(d * d for d in downs) / max(1, len(downs) - 1)) * math.sqrt(252)
    if dd == 0:
        return None
    return (c - rf) / dd


def max_drawdown(series, years=None):
    pts = series
    if years is not None and series:
        start_d = series[-1][0] - timedelta(days=int(years * 365.25))
        pts = [p for p in series if p[0] >= start_d]
    peak, mdd = -1.0, 0.0
    for _, v in pts:
        peak = max(peak, v)
        if peak > 0:
            mdd = min(mdd, v / peak - 1.0)
    return mdd  # negative number, e.g. -0.34


def monthly_returns(series, months_back):
    """Calendar-month-end returns for capture/alpha regressions. Deterministic:
    month-end = last observation of each calendar month."""
    by_month = {}
    for d, v in series:
        by_month[(d.year, d.month)] = v  # ascending -> last obs wins
    keys = sorted(by_month.keys())[-(months_back + 1):]
    rets = []
    for i in range(1, len(keys)):
        v0, v1 = by_month[keys[i - 1]], by_month[keys[i]]
        if v0 > 0:
            rets.append((keys[i], v1 / v0 - 1.0))
    return dict(rets)


def capture_ratios(fund_series, bench_series, months_back):
    f = monthly_returns(fund_series, months_back)
    b = monthly_returns(bench_series, months_back)
    common = sorted(set(f) & set(b))
    up_f = [f[k] for k in common if b[k] > 0]
    up_b = [b[k] for k in common if b[k] > 0]
    dn_f = [f[k] for k in common if b[k] < 0]
    dn_b = [b[k] for k in common if b[k] < 0]
    def geo(x):
        prod = 1.0
        for r in x:
            prod *= (1 + r)
        return prod ** (1.0 / len(x)) - 1.0 if x else None
    up = geo(up_f) / geo(up_b) if up_b and geo(up_b) not in (None, 0) else None
    dn = geo(dn_f) / geo(dn_b) if dn_b and geo(dn_b) not in (None, 0) else None
    return (r6(up), r6(dn), len(common))


def annualised_alpha(fund_series, ref_series, years, as_of):
    start_d = as_of - timedelta(days=int(years * 365.25))
    cf = cagr_between(fund_series, start_d, as_of)
    cr = cagr_between(ref_series, start_d, as_of)
    if cf is None or cr is None:
        return None
    return cf - cr


# ----------------------------------------------------------------------------
# Category composite (equal-weight, deterministic)
# ----------------------------------------------------------------------------

def category_composite(histories, as_of, min_years):
    """Equal-weighted composite of all category members with >= min_years of
    history at as_of. Built from monthly returns to tolerate missing days.
    Returns a synthetic monthly series [(date,value)]."""
    need_months = int(min_years * 12)
    members = []
    for s in histories:
        s = clip(s, as_of)
        if s and (as_of - s[0][0]).days >= min_years * 365.25:
            members.append(monthly_returns(s, need_months))
    if not members:
        return None, 0
    keys = sorted(set().union(*[set(m) for m in members]))
    value, series = 100.0, []
    for k in keys:
        rs = [m[k] for m in members if k in m]
        if not rs:
            continue
        value *= (1 + sum(rs) / len(rs))
        series.append((date(k[0], k[1], 28), value))  # fixed day: deterministic
    return series, len(members)


# ----------------------------------------------------------------------------
# Overlap
# ----------------------------------------------------------------------------

def load_holdings(path):
    w = {}
    with open(path, "r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            isin = row["isin"].strip().upper()
            w[isin] = w.get(isin, 0.0) + float(row["weight_pct"])
    return w


def pairwise_overlap(wa, wb):
    return r6(sum(min(wa[i], wb[i]) for i in set(wa) & set(wb)))


# ----------------------------------------------------------------------------
# The framework
# ----------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "framework_version": FRAMEWORK_VERSION,
    "as_of": None,                      # null -> latest date common to all inputs (recorded)
    "risk_free_rate": 0.065,
    "universe": {
        "plan": "Direct",
        "option": "Growth",
        "allowed_categories": [
            "Equity Scheme - Large Cap Fund",
            "Equity Scheme - Large & Mid Cap Fund",
            "Equity Scheme - Flexi Cap Fund",
            "Equity Scheme - Mid Cap Fund",
            "Equity Scheme - Small Cap Fund",
            "Equity Scheme - Focused Fund",
            "Equity Scheme - Value Fund",
        ],
        "min_history_years": 7,
        "min_aum_cr": None,   # optional; needs offline catalog AUM
    },
    "gates": {                          # ALL must pass; capital-protection first
        "rolling3y_pct_positive_min": 90.0,   # % of 3Y windows with CAGR > 0
        "rolling5y_worst_min": 0.0,           # worst 5Y rolling CAGR must be >= 0
        "alpha_vs_category_3y_min": 0.0,      # annualised, must exceed
        "alpha_vs_category_5y_min": 0.0,
        "alpha_vs_index_3y_min": 0.0,         # only enforced when benchmark supplied
        "alpha_vs_index_5y_min": 0.0,
        "downside_capture_max": 1.00,         # <= 100% of index in down months
        "max_drawdown_vs_category_tolerance": 1.10,  # fund MDD <= 1.10 x category MDD
    },
    "scoring": {                        # weights sum to 100; applied to percentile ranks
        "weights": {
            "rolling5y_median_alpha_vs_category": 25,
            "information_ratio_3y": 20,
            "sortino_5y": 15,
            "downside_capture_inv": 15,
            "max_drawdown_inv": 10,
            "upside_capture": 10,
            "consistency_pct_3y_windows_beating_category": 5,
        },
        "capture_months": 36,
    },
    "selection": {
        "target_count": 4,
        "max_pairwise_overlap_pct": 10.0,
        "max_funds_per_amc": 1,
        "max_funds_per_category": 1,     # proxy diversifier; also applies when holdings exist
        "require_holdings_for_overlap": False,  # True -> refuse to select without holdings data
    },
    "tie_break_order": ["score", "rolling5y_median", "sortino_5y", "scheme_code_asc"],
}


def percentile_ranks(values):
    """values: list of (key, float|None). Deterministic percentile rank in [0,100].
    None -> 0. Ties share the average rank (computed deterministically)."""
    known = sorted([(v, k) for k, v in values if v is not None])
    n = len(known)
    ranks = {}
    i = 0
    while i < n:
        j = i
        while j + 1 < n and known[j + 1][0] == known[i][0]:
            j += 1
        avg_pos = (i + j) / 2.0
        pr = 100.0 * (avg_pos + 0.5) / n if n > 0 else 0.0
        for t in range(i, j + 1):
            ranks[known[t][1]] = r6(pr)
        i = j + 1
    for k, v in values:
        if v is None:
            ranks[k] = 0.0
    return ranks


class Framework:
    def __init__(self, dataset_dir, config, benchmarks_dir=None, holdings_dir=None):
        self.dataset_dir = dataset_dir
        self.cfg = config
        self.benchmarks_dir = benchmarks_dir
        self.holdings_dir = holdings_dir
        self.notes = []          # honesty rail: every assumption logged
        self.input_hashes = {}

    # ---------------- data loading ----------------
    def load(self):
        cat_path = os.path.join(self.dataset_dir, "catalog.json")
        with open(cat_path, "r", encoding="utf-8") as f:
            self.catalog = json.load(f)
        self.input_hashes["catalog.json"] = sha256_file(cat_path)

        u = self.cfg["universe"]
        self.universe = []
        for s in self.catalog:
            if s.get("plan") != u["plan"] or s.get("option") != u["option"]:
                continue
            if s.get("scheme_category") not in u["allowed_categories"]:
                continue
            hp = os.path.join(self.dataset_dir, "history", f"{s['scheme_code']}.json")
            if not os.path.exists(hp):
                continue
            self.universe.append(s)
        self.universe.sort(key=lambda s: s["scheme_code"])  # stable base order

        self.histories = {}
        for s in self.universe:
            hp = os.path.join(self.dataset_dir, "history", f"{s['scheme_code']}.json")
            _, series = load_history(hp)
            self.histories[s["scheme_code"]] = series
            self.input_hashes[f"history/{s['scheme_code']}.json"] = sha256_file(hp)

        # as_of: pinned in config, else latest date present in EVERY history
        if self.cfg.get("as_of"):
            self.as_of = parse_date(self.cfg["as_of"])
            self.notes.append(f"as_of pinned by config: {self.as_of.isoformat()}")
        else:
            last_dates = [h[-1][0] for h in self.histories.values() if h]
            self.as_of = min(last_dates)
            self.notes.append(
                f"as_of derived as min(latest NAV date across universe) = "
                f"{self.as_of.isoformat()}; pin it in config to freeze the snapshot."
            )
        for c in list(self.histories):
            self.histories[c] = clip(self.histories[c], self.as_of)

        # benchmarks
        self.benchmarks = {}
        if self.benchmarks_dir and os.path.isdir(self.benchmarks_dir):
            for fn in sorted(os.listdir(self.benchmarks_dir)):
                if fn.endswith(".csv"):
                    key = fn[:-4]
                    p = os.path.join(self.benchmarks_dir, fn)
                    self.benchmarks[key] = clip(load_benchmark_csv(p), self.as_of)
                    self.input_hashes[f"benchmarks/{fn}"] = sha256_file(p)
        if not self.benchmarks:
            self.notes.append(
                "NO BENCHMARK SERIES SUPPLIED: alpha-vs-index and capture gates are "
                "SKIPPED (marked not-evaluated, NOT passed-by-default in spirit — "
                "see per-fund flags). Supply TRI CSVs to enforce them."
            )

        # holdings
        self.holdings = {}
        if self.holdings_dir and os.path.isdir(self.holdings_dir):
            for fn in sorted(os.listdir(self.holdings_dir)):
                if fn.endswith(".csv"):
                    code = fn[:-4]
                    p = os.path.join(self.holdings_dir, fn)
                    try:
                        self.holdings[int(code)] = load_holdings(p)
                        self.input_hashes[f"holdings/{fn}"] = sha256_file(p)
                    except ValueError:
                        pass
        if not self.holdings:
            self.notes.append(
                "NO HOLDINGS FILES SUPPLIED: pairwise overlap cannot be computed. "
                "Falling back to max 1 fund per SEBI category + 1 per AMC as a "
                "diversification proxy. This does NOT verify <10% overlap."
            )

    def benchmark_for(self, scheme):
        """Deterministic mapping category -> benchmark key, from config; else None."""
        mapping = self.cfg.get("benchmark_map", {})
        key = mapping.get(scheme["scheme_category"])
        return self.benchmarks.get(key) if key else None

    # ---------------- metrics ----------------
    def compute_metrics(self):
        u = self.cfg["universe"]
        min_years = u["min_history_years"]

        # category composites
        by_cat = {}
        for s in self.universe:
            by_cat.setdefault(s["scheme_category"], []).append(
                self.histories[s["scheme_code"]]
            )
        self.cat_composites, self.cat_sizes = {}, {}
        for cat, hs in sorted(by_cat.items()):
            comp, n = category_composite(hs, self.as_of, min_years)
            self.cat_composites[cat] = comp
            self.cat_sizes[cat] = n
        self.notes.append(
            "Category composites are equal-weighted over funds ALIVE TODAY -> "
            "survivorship bias flatters the category average; real category "
            "averages (incl. dead funds) would be lower, so alpha-vs-category "
            "here is a CONSERVATIVE hurdle."
        )

        self.metrics = {}
        for s in self.universe:
            code = s["scheme_code"]
            series = self.histories[code]
            m = {"scheme_code": code, "history_years": None}
            if series:
                m["history_years"] = r6((self.as_of - series[0][0]).days / 365.25)

            r3 = rolling_cagrs(series, 3)
            r5 = rolling_cagrs(series, 5)
            m["rolling3y_n"] = len(r3)
            m["rolling3y_pct_positive"] = (
                r6(100.0 * sum(1 for x in r3 if x > 0) / len(r3)) if r3 else None
            )
            m["rolling5y_median"] = (
                r6(sorted(r5)[len(r5) // 2]) if r5 else None
            )
            m["rolling5y_worst"] = r6(min(r5)) if r5 else None
            m["max_drawdown_5y"] = r6(max_drawdown(series, years=5))
            m["sortino_5y"] = r6(sortino(series, self.cfg["risk_free_rate"], 5))
            m["vol_ann"] = r6(annualised_vol(series))

            # vs category composite
            cat = s["scheme_category"]
            comp = self.cat_composites.get(cat)
            if comp:
                m["alpha_cat_3y"] = r6(annualised_alpha(series, comp, 3, self.as_of))
                m["alpha_cat_5y"] = r6(annualised_alpha(series, comp, 5, self.as_of))
                comp_r5 = rolling_cagrs(comp, 5, step_days=30)
                if r5 and comp_r5:
                    k = min(len(r5), len(comp_r5))
                    diffs = [r5[i] - comp_r5[i] for i in range(k)]
                    m["rolling5y_median_alpha_vs_category"] = r6(
                        sorted(diffs)[len(diffs) // 2]
                    )
                    r3c = rolling_cagrs(comp, 3, step_days=30)
                    k3 = min(len(r3), len(r3c))
                    m["consistency_pct_3y_windows_beating_category"] = (
                        r6(100.0 * sum(1 for i in range(k3) if r3[i] > r3c[i]) / k3)
                        if k3 else None
                    )
                m["cat_mdd_5y"] = r6(max_drawdown(comp, years=5))
            else:
                for k in ("alpha_cat_3y", "alpha_cat_5y",
                          "rolling5y_median_alpha_vs_category",
                          "consistency_pct_3y_windows_beating_category", "cat_mdd_5y"):
                    m[k] = None

            # vs index (only if supplied)
            bench = self.benchmark_for(s)
            if bench:
                m["alpha_idx_3y"] = r6(annualised_alpha(series, bench, 3, self.as_of))
                m["alpha_idx_5y"] = r6(annualised_alpha(series, bench, 5, self.as_of))
                up, dn, npts = capture_ratios(
                    series, bench, self.cfg["scoring"]["capture_months"]
                )
                m["upside_capture"], m["downside_capture"] = up, dn
                m["capture_points"] = npts
                # information ratio (monthly active returns, 3y)
                f = monthly_returns(series, 36)
                b = monthly_returns(bench, 36)
                common = sorted(set(f) & set(b))
                if len(common) >= 24:
                    act = [f[k] - b[k] for k in common]
                    mu = sum(act) / len(act)
                    sd = math.sqrt(
                        sum((a - mu) ** 2 for a in act) / (len(act) - 1)
                    )
                    m["information_ratio_3y"] = (
                        r6(mu * 12 / (sd * math.sqrt(12))) if sd > 0 else None
                    )
                else:
                    m["information_ratio_3y"] = None
                m["benchmark_evaluated"] = True
            else:
                for k in ("alpha_idx_3y", "alpha_idx_5y", "upside_capture",
                          "downside_capture", "information_ratio_3y"):
                    m[k] = None
                m["benchmark_evaluated"] = False

            self.metrics[code] = m

    # ---------------- gates ----------------
    def apply_gates(self):
        g = self.cfg["gates"]
        u = self.cfg["universe"]
        self.gate_results = {}
        for s in self.universe:
            code = s["scheme_code"]
            m = self.metrics[code]
            checks = {}
            checks["history"] = (
                m["history_years"] is not None
                and m["history_years"] >= u["min_history_years"]
            )
            checks["rolling3y_positive"] = (
                m["rolling3y_pct_positive"] is not None
                and m["rolling3y_pct_positive"] >= g["rolling3y_pct_positive_min"]
            )
            checks["rolling5y_worst"] = (
                m["rolling5y_worst"] is not None
                and m["rolling5y_worst"] >= g["rolling5y_worst_min"]
            )
            checks["alpha_cat_3y"] = (
                m["alpha_cat_3y"] is not None
                and m["alpha_cat_3y"] > g["alpha_vs_category_3y_min"]
            )
            checks["alpha_cat_5y"] = (
                m["alpha_cat_5y"] is not None
                and m["alpha_cat_5y"] > g["alpha_vs_category_5y_min"]
            )
            if m.get("cat_mdd_5y") is not None and m["max_drawdown_5y"] is not None:
                checks["drawdown_vs_category"] = (
                    m["max_drawdown_5y"]
                    >= m["cat_mdd_5y"] * g["max_drawdown_vs_category_tolerance"]
                )
            else:
                checks["drawdown_vs_category"] = False
            if m["benchmark_evaluated"]:
                checks["alpha_idx_3y"] = (
                    m["alpha_idx_3y"] is not None
                    and m["alpha_idx_3y"] > g["alpha_vs_index_3y_min"]
                )
                checks["alpha_idx_5y"] = (
                    m["alpha_idx_5y"] is not None
                    and m["alpha_idx_5y"] > g["alpha_vs_index_5y_min"]
                )
                checks["downside_capture"] = (
                    m["downside_capture"] is not None
                    and m["downside_capture"] <= g["downside_capture_max"]
                )
            self.gate_results[code] = {
                "checks": checks,
                "passed": all(checks.values()),
                "benchmark_gates_evaluated": m["benchmark_evaluated"],
            }

    # ---------------- scoring ----------------
    def score(self):
        w = self.cfg["scoring"]["weights"]
        survivors = [
            s for s in self.universe if self.gate_results[s["scheme_code"]]["passed"]
        ]
        codes = [s["scheme_code"] for s in survivors]

        def col(name, invert=False):
            vals = []
            for c in codes:
                v = self.metrics[c].get(name)
                vals.append((c, (-v if (invert and v is not None) else v)))
            return percentile_ranks(vals)

        cols = {
            "rolling5y_median_alpha_vs_category": col("rolling5y_median_alpha_vs_category"),
            "information_ratio_3y": col("information_ratio_3y"),
            "sortino_5y": col("sortino_5y"),
            "downside_capture_inv": col("downside_capture", invert=True),
            "max_drawdown_inv": col("max_drawdown_5y"),  # MDD is negative; higher (closer to 0) is better
            "upside_capture": col("upside_capture"),
            "consistency_pct_3y_windows_beating_category": col(
                "consistency_pct_3y_windows_beating_category"
            ),
        }
        total_w = sum(w.values())
        self.scores = {}
        for c in codes:
            sc = sum(w[k] * cols[k][c] for k in w) / total_w
            self.scores[c] = r6(sc)
        self.survivors = survivors

    # ---------------- selection ----------------
    def sort_key(self, s):
        c = s["scheme_code"]
        m = self.metrics[c]
        return (
            -(self.scores.get(c, 0.0)),
            -(m.get("rolling5y_median") or -999),
            -(m.get("sortino_5y") or -999),
            c,  # absolute final tie-break: scheme_code ascending
        )

    def select(self):
        sel_cfg = self.cfg["selection"]
        ranked = sorted(self.survivors, key=self.sort_key)
        picked, decisions = [], []
        have_holdings = bool(self.holdings)
        if sel_cfg["require_holdings_for_overlap"] and not have_holdings:
            self.notes.append(
                "selection.require_holdings_for_overlap=True and no holdings "
                "supplied -> NO SELECTION MADE (ranking only)."
            )
            self.selection, self.decisions = [], decisions
            self.ranked = ranked
            return

        for s in ranked:
            if len(picked) >= sel_cfg["target_count"]:
                break
            code = s["scheme_code"]
            reason = None
            if sel_cfg["max_funds_per_amc"] and sum(
                1 for p in picked if p["fund_house"] == s["fund_house"]
            ) >= sel_cfg["max_funds_per_amc"]:
                reason = "amc_limit"
            elif sel_cfg["max_funds_per_category"] and sum(
                1 for p in picked if p["scheme_category"] == s["scheme_category"]
            ) >= sel_cfg["max_funds_per_category"]:
                reason = "category_limit"
            elif have_holdings:
                wa = self.holdings.get(code)
                if wa is None:
                    reason = "holdings_missing_for_fund"
                else:
                    for p in picked:
                        wb = self.holdings.get(p["scheme_code"])
                        ov = pairwise_overlap(wa, wb)
                        if ov > sel_cfg["max_pairwise_overlap_pct"]:
                            reason = f"overlap_{ov}pct_with_{p['scheme_code']}"
                            break
            if reason:
                decisions.append({"scheme_code": code, "action": "skipped", "reason": reason})
            else:
                picked.append(s)
                decisions.append({"scheme_code": code, "action": "selected"})
        self.selection, self.decisions, self.ranked = picked, decisions, ranked

    # ---------------- output ----------------
    def run(self):
        self.load()
        self.compute_metrics()
        self.apply_gates()
        self.score()
        self.select()
        return self.report()

    def report(self):
        overlap_matrix = {}
        if self.holdings and len(self.selection) > 1:
            for i, a in enumerate(self.selection):
                for b in self.selection[i + 1:]:
                    wa, wb = self.holdings.get(a["scheme_code"]), self.holdings.get(b["scheme_code"])
                    if wa and wb:
                        overlap_matrix[f"{a['scheme_code']}x{b['scheme_code']}"] = pairwise_overlap(wa, wb)
        rep = {
            "framework_version": FRAMEWORK_VERSION,
            "as_of": self.as_of.isoformat(),
            "config_hash": sha256_obj(self.cfg),
            "input_hashes": self.input_hashes,
            "universe_size": len(self.universe),
            "category_composite_sizes": self.cat_sizes,
            "gates_passed": sum(1 for g in self.gate_results.values() if g["passed"]),
            "ranking": [
                {
                    "rank": i + 1,
                    "scheme_code": s["scheme_code"],
                    "scheme_name": s["scheme_name"],
                    "fund_house": s["fund_house"],
                    "category": s["scheme_category"],
                    "score": self.scores.get(s["scheme_code"]),
                    "metrics": self.metrics[s["scheme_code"]],
                }
                for i, s in enumerate(self.ranked)
            ],
            "selection": [s["scheme_code"] for s in self.selection],
            "selection_decisions": self.decisions,
            "overlap_matrix_pct": overlap_matrix,
            "gate_results": self.gate_results,
            "notes_and_caveats": self.notes + [
                "Past performance does not predict future returns; mutual fund "
                "investments are subject to market risk.",
                "Determinism guarantee: identical config_hash + input_hashes -> "
                "identical report. If a re-run differs, diff the hashes first.",
            ],
        }
        # run_hash proves reproducibility (excludes itself)
        rep["run_hash"] = sha256_obj(rep)
        return rep


# ----------------------------------------------------------------------------
# Selftest: synthetic universe, verifies determinism + overlap + gates
# ----------------------------------------------------------------------------

def _make_series(start, days, drift_daily, wobble_amp, wobble_period, crash_day=None, crash_size=0.0):
    """Fully deterministic synthetic NAV path (no RNG)."""
    out, v = [], 100.0
    d = start
    for i in range(days):
        w = wobble_amp * math.sin(2 * math.pi * i / wobble_period)
        r = drift_daily + w
        if crash_day is not None and i == crash_day:
            r = crash_size
        v *= (1 + r)
        out.append((d, round(v, 4)))
        d += timedelta(days=1)
    return out


def selftest():
    import tempfile, shutil
    tmp = tempfile.mkdtemp()
    try:
        ds = os.path.join(tmp, "mf_dataset")
        os.makedirs(os.path.join(ds, "history"))
        start = date(2015, 1, 1)
        days = 4200  # ~11.5y
        cat = "Equity Scheme - Flexi Cap Fund"
        specs = {
            # code: (drift, wobble, crash)  — 1..4 good funds, 5 weak, 6 crashy
            101: (0.00060, 0.004, None),
            102: (0.00055, 0.004, None),
            103: (0.00052, 0.004, None),
            104: (0.00054, 0.004, None),
            105: (0.00030, 0.004, None),          # below category -> alpha gate fail
            106: (0.00058, 0.004, (2000, -0.60)), # deep crash -> drawdown/rolling gate fail
        }
        catalog = []
        for code, (drift, wob, crash) in specs.items():
            series = _make_series(
                start, days, drift, wob, 500,
                crash_day=crash[0] if crash else None,
                crash_size=crash[1] if crash else 0.0,
            )
            with open(os.path.join(ds, "history", f"{code}.json"), "w") as f:
                json.dump({"meta": {"scheme_code": code},
                           "nav": [{"date": d.isoformat(), "nav": v} for d, v in series]}, f)
            catalog.append({
                "scheme_code": code,
                "scheme_name": f"Test Fund {code} - Direct Plan-Growth",
                "fund_house": f"AMC {code % 3}",   # forces AMC-limit collisions
                "scheme_type": "Open Ended Schemes",
                "scheme_category": cat,
                "plan": "Direct", "option": "Growth",
            })
        with open(os.path.join(ds, "catalog.json"), "w") as f:
            json.dump(catalog, f)

        # benchmark: slightly below the best funds
        bench = _make_series(start, days, 0.00048, 0.004, 500)
        bdir = os.path.join(tmp, "benchmarks")
        os.makedirs(bdir)
        with open(os.path.join(bdir, "NIFTY500TRI.csv"), "w", newline="") as f:
            wtr = csv.writer(f); wtr.writerow(["date", "value"])
            for d, v in bench:
                wtr.writerow([d.isoformat(), v])

        # holdings: 101&102 overlap heavily (should exclude one), others distinct
        hdir = os.path.join(tmp, "holdings")
        os.makedirs(hdir)
        H = {
            101: {"INE001": 40, "INE002": 30, "INE003": 30},
            102: {"INE001": 35, "INE002": 35, "INE004": 30},   # overlap with 101 = 65
            103: {"INE010": 50, "INE011": 50},                 # overlap 0
            104: {"INE020": 50, "INE003": 5, "INE021": 45},    # overlap with 101 = 5
            105: {"INE030": 100},
            106: {"INE040": 100},
        }
        for code, w in H.items():
            with open(os.path.join(hdir, f"{code}.csv"), "w", newline="") as f:
                wtr = csv.writer(f); wtr.writerow(["isin", "weight_pct"])
                for isin, wt in w.items():
                    wtr.writerow([isin, wt])

        cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        cfg["benchmark_map"] = {cat: "NIFTY500TRI"}
        cfg["selection"]["max_funds_per_category"] = None   # single-category selftest
        cfg["selection"]["max_funds_per_amc"] = None
        cfg["selection"]["target_count"] = 3
        cfg["as_of"] = "2026-06-30"

        fw1 = Framework(ds, cfg, bdir, hdir); rep1 = fw1.run()
        fw2 = Framework(ds, cfg, bdir, hdir); rep2 = fw2.run()

        assert rep1["run_hash"] == rep2["run_hash"], "DETERMINISM FAILED"
        assert rep1["gates_passed"] >= 3, f"expected >=3 gate survivors, got {rep1['gates_passed']}"
        failed_codes = [c for c, g in rep1["gate_results"].items() if not g["passed"]]
        assert 105 in failed_codes, "weak fund 105 should fail alpha-vs-category gate"
        assert 106 in failed_codes, "crash fund 106 should fail rolling/drawdown gates"
        sel = rep1["selection"]
        assert not (101 in sel and 102 in sel), "overlap 65% pair must not co-exist in selection"
        for k, v in rep1["overlap_matrix_pct"].items():
            assert v <= cfg["selection"]["max_pairwise_overlap_pct"], f"overlap breach {k}={v}"
        # a third run with a changed config must change config_hash
        cfg2 = json.loads(json.dumps(cfg)); cfg2["risk_free_rate"] = 0.07
        rep3 = Framework(ds, cfg2, bdir, hdir).run()
        assert rep3["config_hash"] != rep1["config_hash"]

        print("SELFTEST PASS")
        print(f"  universe={rep1['universe_size']} gates_passed={rep1['gates_passed']}")
        print(f"  selection={sel}")
        print(f"  overlap_matrix={rep1['overlap_matrix_pct']}")
        print(f"  run_hash={rep1['run_hash'][:16]}... (identical across both runs)")
        return 0
    finally:
        shutil.rmtree(tmp)


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Deterministic MF selection framework")
    ap.add_argument("--dataset")
    ap.add_argument("--config")
    ap.add_argument("--benchmarks")
    ap.add_argument("--holdings")
    ap.add_argument("--out", default="mf_select_out")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        sys.exit(selftest())

    if not args.dataset:
        ap.error("--dataset is required (or use --selftest)")
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if args.config:
        with open(args.config, "r", encoding="utf-8") as f:
            user_cfg = json.load(f)
        def merge(base, over):
            for k, v in over.items():
                if isinstance(v, dict) and isinstance(base.get(k), dict):
                    merge(base[k], v)
                else:
                    base[k] = v
        merge(cfg, user_cfg)

    fw = Framework(args.dataset, cfg, args.benchmarks, args.holdings)
    rep = fw.run()
    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "report.json"), "w", encoding="utf-8") as f:
        json.dump(rep, f, indent=2, sort_keys=True)
    print(f"as_of={rep['as_of']} universe={rep['universe_size']} "
          f"gates_passed={rep['gates_passed']} selection={rep['selection']}")
    print(f"run_hash={rep['run_hash']}")
    print(f"report -> {os.path.join(args.out, 'report.json')}")


if __name__ == "__main__":
    main()
