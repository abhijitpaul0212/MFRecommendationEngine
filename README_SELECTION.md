# MFRecommendationEngine — Deterministic Selection Framework

Rules-based, reproducible selection of 3–4 Direct-Growth mutual funds for a
long-term (>10Y) horizon, with hard capital-protection gates, alpha measured
against both the category average and a supplied benchmark TRI, and
holdings-based pairwise overlap enforcement.

> **Disclaimer:** This produces data and arithmetic, not investment advice.
> Past performance does not predict future returns. Mutual fund investments
> are subject to market risk.

## Layout

```
selection/
├── mf_select.py            # the framework (stdlib only, no dependencies)
└── framework_config.json   # all rules: universe, gates, weights, selection
docs/
└── FRAMEWORK.md            # full specification and honesty notes
tests/
└── test_mf_select.py       # pytest wrapper over the deterministic selftest
benchmarks/                  # you supply: <KEY>.csv with date,value (TRI!)
holdings/                    # you supply: <scheme_code>.csv with isin,weight_pct
.github/workflows/
└── framework-ci.yml         # CI: selftest + pytest on 3.10 and 3.12
```

## Quick start

```bash
# 1. Verify the framework's own guarantees (no data needed)
python selection/mf_select.py --selftest

# 2. Build the dataset (from the indian-mf-data tooling)
python scripts/build_mf_dataset.py --out mf_dataset

# 3. Run a selection
python selection/mf_select.py \
    --dataset mf_dataset \
    --config selection/framework_config.json \
    --benchmarks benchmarks/ \
    --holdings holdings/ \
    --out runs/2026-07
```

The report (`runs/2026-07/report.json`) contains the full ranking, per-gate
results, the selection with a decision log for every skip, the overlap matrix,
and a `run_hash` + `config_hash` + per-file `input_hashes` manifest.

## Reproducibility contract

Same config hash + same input hashes ⇒ identical `run_hash`, always. If two
runs differ, diff the hashes first: it will be the data (new NAV dates, new
holdings month), never the logic. Pin `"as_of"` in the config to freeze a
snapshot for repeatable audits. CI re-proves determinism on every push across
two Python versions.

## What you must supply (and why the framework won't guess)

- **Benchmark TRI CSVs** (`benchmarks/NIFTY500_TRI.csv`, columns `date,value`)
  from niftyindices.com — NAV data contains no index, and using a price index
  instead of TRI fabricates ~1.2–1.5%/yr of fake alpha. Without these, the
  alpha-vs-index and capture gates are marked *not evaluated* in the report.
- **Holdings CSVs** (`holdings/<scheme_code>.csv`, columns `isin,weight_pct`)
  from AMC monthly portfolio disclosures. Without these, pairwise overlap
  cannot be computed and the framework falls back to one-fund-per-category /
  one-per-AMC and says so — it never silently claims "<10% overlap".

See `docs/FRAMEWORK.md` for the full rule set, determinism design, and the
honest limits (survivorship bias in category composites, realistic overlap
thresholds, Direct-plan history starting 2013).
