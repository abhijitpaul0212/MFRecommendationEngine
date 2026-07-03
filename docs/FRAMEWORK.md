# Deterministic Direct-MF Selection Framework (>10Y horizon)

Version 1.0.0 — companion spec for `mf_select.py`. Designed to sit on top of the
`indian-mf-data` dataset layer (AMFI catalog + mfapi.in NAV histories) and slot
into MFRecommendationEngine as the selection module.

## What the framework promises — and what it honestly cannot

The framework guarantees **reproducibility**: given the same data snapshot, config,
benchmark files, and holdings files, every run produces a byte-identical ranking and
selection, provable via the `run_hash` in the report. It does **not** promise the
same output across different days, because NAVs update daily — that is the data
changing, not the logic drifting. Pin `as_of` in the config to freeze a snapshot;
the manifest's `config_hash` and `input_hashes` tell you exactly *why* two runs
differ if they ever do.

Equally important, it does not pretend to know things NAV data cannot tell it.
Alpha versus an index requires a Total Returns Index series you supply as CSV
(NAVs contain no benchmark; fabricating one would poison every downstream number).
Portfolio overlap requires holdings disclosures you supply as CSV per fund; without
them the framework applies a category/AMC exclusion proxy and says so in the report
rather than silently claiming "<10% overlap". And the category-average hurdle is
built from funds alive today, which means survivorship bias makes it a *harder*
hurdle than the true historical category average — a conservative bias, flagged in
every report.

One more piece of honesty you should carry into your expectations: a sub-10%
pairwise overlap between two Indian *equity* funds is only realistically achievable
across genuinely different mandates (e.g. large cap × small cap × international ×
value). Two flexi-cap funds from different AMCs routinely overlap 25–45% because
they all hold the same index heavyweights. The framework enforces whatever
threshold you set, but if you set 10% within similar categories it may honestly
return fewer than 4 funds rather than fake a diversified basket — that is by
design. Treat 10% as a cross-category target, or relax to ~25–33% (the common
industry heuristic) if you want 4 funds from adjacent categories.

## Pipeline

The run is a strict five-stage pipe; a fund can never score its way past a failed
gate, and selection can never override an overlap breach.

**Stage 1 — Universe.** Filter the catalog to Direct + Growth, allowed SEBI equity
categories, minimum 7 years of NAV history (long-horizon judgment needs at least
one full cycle; 10 years is better if you can afford the smaller universe). The
universe is sorted by `scheme_code` before anything else so iteration order is
fixed.

**Stage 2 — Metrics.** For each fund: 3Y and 5Y rolling CAGRs (30-day step, windows
anchored to the series end — fully deterministic), 5Y max drawdown, 5Y Sortino,
annualised volatility, alpha vs the equal-weighted category composite (3Y and 5Y,
point-to-point and rolling-median), consistency (% of 3Y windows beating the
category), and — when a benchmark TRI is mapped — alpha vs index (3Y/5Y), 36-month
up/down capture, and a 3Y information ratio. Every metric is rounded to 6 dp
*before* any comparison so float noise can never flip an ordering across
platforms.

**Stage 3 — Hard gates (capital protection).** All must pass:

| Gate | Default | Rationale |
| --- | --- | --- |
| ≥90% of 3Y rolling windows positive | 90% | removes funds that lose money in ordinary 3Y holds |
| Worst 5Y rolling CAGR ≥ 0 | 0% | for a >10Y horizon, a fund should never have destroyed capital over any 5Y stretch |
| Alpha vs category composite > 0 (3Y and 5Y) | 0 | your "beat category average" mandate, on a survivorship-conservative hurdle |
| Alpha vs index > 0 (3Y and 5Y) | 0 | your "beat index" mandate — enforced only when TRI supplied |
| Downside capture ≤ 100% | 1.00 | falls no harder than the index in down months |
| Max drawdown ≤ 1.10 × category drawdown | 1.10 | drawdown control relative to peers |

**Stage 4 — Scoring.** Survivors are ranked on a 0–100 composite of percentile
ranks with fixed weights: rolling-5Y median alpha vs category 25, information
ratio 20, Sortino 15, inverse downside capture 15, inverse max drawdown 10,
upside capture 10, consistency 5. Percentile ranking (rather than raw z-scores)
makes the score robust to outliers and scale-free across categories; ties share an
average rank computed deterministically. Note the weighting is deliberately
risk-heavy: 40 points reward *not losing* (downside capture, drawdown, Sortino's
denominator) versus pure return-seeking — that is the capital-protection mandate
expressed numerically.

**Stage 5 — Overlap-constrained selection.** Walk the ranking top-down and pick a
fund only if it (a) doesn't breach the per-AMC limit, (b) doesn't breach the
per-category limit, and (c) has pairwise holdings overlap ≤ threshold with every
already-picked fund, where overlap = Σ min(weightA, weightB) over common ISINs.
Skipped funds are logged with the exact reason (e.g. `overlap_65.0pct_with_101`)
so the audit trail shows *why* rank-2 isn't in the basket. Ties anywhere in the
ranking break on score → rolling-5Y median → Sortino → `scheme_code` ascending,
which is a total order: no two funds can ever tie completely, so the output is
unique.

## Determinism guarantees, concretely

No randomness exists anywhere in the code path — no RNG, no set-iteration-order
dependence (every dict/set is sorted before iteration), no wall-clock reads. The
as-of date is either pinned in config or derived as the minimum of the latest NAV
dates across the universe (and recorded, with a note telling you to pin it). All
floats are rounded before comparison. The final tie-break on `scheme_code` makes
the ordering total. The report embeds `config_hash` (SHA-256 of the canonicalised
config), `input_hashes` (SHA-256 of every catalog/history/benchmark/holdings file
consumed), and `run_hash` (SHA-256 of the whole report). Two runs agree ⇔ their
run_hashes agree, and the built-in `--selftest` asserts exactly that across two
independent executions, plus the gate and overlap behaviours, on a synthetic
no-RNG universe.

## Data you need to supply

The dataset comes from the existing skill: `python build_mf_dataset.py --out
mf_dataset` (screen everything), then `--codes <shortlist>` for full histories.
Benchmarks go in `benchmarks/<KEY>.csv` with columns `date,value`; use **TRI**
(total returns) series, not price indices — comparing dividend-reinvested NAVs to a
price index manufactures ~1.2–1.5% of fake alpha per year. Niftyindices.com
publishes downloadable TRI histories. Holdings go in `holdings/<scheme_code>.csv`
with columns `isin,weight_pct`, sourced from the AMC's monthly portfolio
disclosure (mandatory SEBI publication) or exported from an overlap tool. Refresh
holdings monthly; the input hash will record which disclosure month a run used.

## Running it

```
python mf_select.py --dataset mf_dataset --config framework_config.json \
    --benchmarks benchmarks/ --holdings holdings/ --out run_2026_07
python mf_select.py --selftest
```

The report (`report.json`) contains the full ranking with per-fund metrics, gate
results per check, the selection with its decision log, the pairwise overlap
matrix of the chosen basket, and all caveats. Nothing is asserted that isn't in
the data.

## Standing caveats (read before every use)

Past performance does not predict future returns; every number here is evidence
about the past, and mutual fund investments are subject to market risk. The
category composite is survivorship-biased (conservatively). Direct plans exist
only since January 2013, so no Direct-plan history can exceed ~13 years yet.
Backtested alpha persistence in Indian equity funds is weak beyond 3–5 years —
re-run the framework annually (same config, new snapshot) and treat a fund
falling out of the gates for two consecutive annual runs as a review trigger, not
an automatic sell (exit loads and capital-gains tax make churn expensive). This
framework produces data and arithmetic, not investment advice.
