---
name: mf-recommend
description: Run the deterministic mutual fund recommendation engine over the enriched Morningstar snapshot in ms_data/ — gates, scoring, bucket-diversified selection with per-fund recommendation reasons. Use when the user asks for fund recommendations, to re-run/refresh the recommendation engine, or to score/rank mutual funds from the scraped data.
---

# Deterministic MF recommendation engine

## Background (read before running)

`selection/mf_recommend.py` is the recommendation engine over the enriched
Morningstar snapshot (produced by the **morningstar-scrape** skill). It never
touches the network — it only reads `ms_data/<Fund_House>.json` files, so its
output is fully reproducible from a pinned snapshot.

The full data dictionary, gate table, scoring weights and the REASONING behind
each is maintained in README.md under "Recommendation engine — knowledge base"
— read that section before changing anything. Summary (engine v1.4.0):
1. **Universe** — only enriched funds (having `risk_ratings`), filtered to
   Direct+Growth plans by name, mapped to buckets:
   core (Flexi/Large/Large&Mid/Focused), growth (Mid-Cap),
   aggressive (Small-Cap), diversifier (Value/Contra/Multi-Asset/hybrids).
2. **Horizon-aware**: `investment_horizon_years` (default 10) decides which
   risk tables LEAD (10y+ -> 10Y>5Y>3Y); alpha STABILITY is always judged
   across all horizons (worst-horizon alpha excess + consistency count).
3. **Gates (capital protection first — nothing scores past a failed gate)**:
   alpha excess vs category > 0; Sharpe >= category AND Sharpe >= 0 (the
   risk-free hurdle — Morningstar Sharpe is already net of risk-free);
   R-Squared >= 70 (below that, alpha/beta are statistical noise);
   bucket beta bands (core 1.00, satellites 1.15, diversifier 1.05) with
   beta > 1.0 requiring alpha excess >= 1.0 as compensation; downside capture
   <= 110 absolute AND <= 1.05× category; drawdown within 1.10× category.
   Incomplete data fails `data_complete` — values are never guessed.
4. **Scoring** — weighted percentile ranks of gate survivors (sum 100):
   alpha_excess 20, worst_alpha_excess 15, sharpe_excess 15, capture_spread 10,
   downside_capture_low 10, drawdown_edge 10, drawdown_recovery_fast 5,
   std_edge 5, portfolio_quality 5, concentration_low 3, turnover_low 2.
5. **Selection** — greedy by score with bucket quotas (1 core / 1 growth /
   1 aggressive / 1 diversifier, then best-remaining fill), max 1 per AMC,
   max 1 per category, pairwise equity-holdings overlap <= 10% (from scraped
   holding names+weights). Every pick carries a `recommendation_reason`
   assembled from the same numbers that drove the gates and score.
6. **v1.4 additions**: per-pick BENCH of pre-validated same-bucket
   substitutes (drop-in-replacement semantics); `--exclude 'Fund'`
   (repeatable) for closed-loop re-selection — the fund stays ranked but is
   barred from picking, logged as `excluded_by_config`, and hashed into
   `config_hash`; holdings-coverage tracking with rigorous worst-case
   overlap upper bounds for truncated tables; near-miss reporting; a
   per-finalist manual verification checklist.

## How to run

```bash
source .venv/bin/activate           # ./setup.sh first if .venv is missing

# 0) (optional, for a FUTURE-DATE refresh) rebuild the data snapshot first
#    via the morningstar-scrape skill, then:

# 1) always prove the engine itself first (synthetic, no data needed)
python selection/mf_recommend.py --selftest        # must print SELFTEST PASS

# 2) run on the real snapshot
python selection/mf_recommend.py --data ms_data --out ms_data/recommendation_run
#    add --exclude 'Fund Name' (repeatable) after a Stage 3 FAIL to rebuild
#    the portfolio under full constraints (closed loop)

# 3) unit tests
python -m pytest tests/test_mf_recommend.py -v
```

**This is Stage 2 of a 5-stage pipeline** (README "Quick guide"). The picks
are NOT final until Stage 3 (`selection/nav_rolling_check.py`) verifies them
against full NAV history — a top-scored pick can still FAIL the rolling gate
(the snapshot cannot see path-dependence). For the full verified
recommend → verify → allocate loop, use the **mf-portfolio-loop agent**
(`.claude/agents/mf-portfolio-loop.md`), which encodes the exclusion-rebuild
procedure and the structural selection heuristics.

Config overrides go in a JSON file passed via `--config` (deep-merged onto
`DEFAULT_CONFIG` in the module — gates, weights, buckets, quotas, overlap cap).

## Model-judgment step (always perform after the engine run)

After the engine run, READ `recommendations.json` and write
`model_judgment.md` in the same output dir, per the contract in README.md
("Model-judgment layer"): open with the run_hash being interpreted; ground
every quantitative claim in numbers from the report (never outside data or
recalled fund lore); do NOT alter scores/picks — record disagreements as
"model flags" for config tuning; cover per fund: skill inference, horizon
fit, risk-free hurdle, alpha stability in best/worst markets, downside
profile (captures + drawdown depth/recovery), holdings hygiene, and a stated
conviction (High/Medium/Low) with basis; close with portfolio-level synthesis
(bucket coverage, overlap, combined risk posture, falsifiers). Present the
same analysis to the user in the chat response.

## Expected output

- `ms_data/recommendation_run/recommendations.json` — full report:
  `ranking` (every universe fund with metrics + per-gate checks),
  `excluded_by_gates` (with the exact failed checks),
  `recommendations` (picks with score, bucket and `recommendation_reason`),
  `bench` (pre-validated substitutes per pick), `near_misses`,
  `selection_decisions` (including `excluded_by_config` for --exclude'd
  funds), `overlap_matrix_pct` + `overlap_upper_bound_pct` +
  `overlap_uncertain_pairs`, `holdings_coverage`,
  `manual_verification_note`, `notes_and_caveats`, and a manifest
  (`engine_version`, `config_hash`, `input_hashes` per house file,
  `run_hash`, `generated_at`).
- `ms_data/recommendation_run/recommendations.md` — human-readable summary.
- `ms_data/recommendation_run/model_judgment.md` — the model-judgment layer
  (see above), bound to the run_hash it interprets.
- Console prints universe size, gates passed, picks and the run_hash.

An honest empty `recommendations` list is a VALID outcome — it means no fund
passed the capital-protection gates or too few houses are enriched. Report it
as such; do not loosen gates to force picks unless the user asks. For a full
universe run, enrich more fund houses first (the engine needs the risk tables).

## Determinism contract

Identical (ms_data snapshot, config) → identical `run_hash`, byte-for-byte
identical ranking/selection: no RNG, no wall-clock in the hashed payload
(`generated_at` is recorded but excluded from run_hash), floats rounded via
r6() before comparison, every ordering ends in a fund-name tie-break. To
re-run at a future date: refresh the snapshot (morningstar-scrape), re-run the
engine, and compare — a changed result is always attributable to changed
`input_hashes` (new data) or `config_hash` (new rules), never to chance. Both
runs' reports remain valid records of their respective snapshots.
