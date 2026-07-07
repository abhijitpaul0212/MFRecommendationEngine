# Mutual Fund Recommendations

- engine: v1.4.0  |  run_hash: `05a33e329751ba69…`
- horizons: 10Y > 5Y > 3Y  |  universe: 380  |  passed gates: 126  |  recommended: 3

## 1. Parag Parikh Flexi Cap Direct Growth  (score 87.230159)
*PPFAS_Asset_Management_Pvt_Ltd — Flexi Cap — core bucket*

core pick (Flexi Cap); 5Y alpha 4.16 vs category 0.61 (+3.55 excess); alpha stability: worst-horizon excess +2.94 and positive in 3/3 horizons; Sharpe 0.79 vs 0.48 (+0.31) (clears the risk-free hurdle); beta 0.64; captures 84.0 of up-markets vs 58.0 of down-markets (spread +26.0); max drawdown -14.01% vs category -18.0% (shallower); recovered from max drawdown in 6 months; holdings avg star 3.391208, top-10 = 51% of assets, turnover 18.81%

**Manual verification before investing:**
- [ ] Sortino ratio: confirm top-quartile vs category (Value Research / Rupeevest / AMC factsheet) — engine uses Sharpe + downside-capture only; Sortino is not on Morningstar's public pages.
- [ ] Manager tenure: confirm the lead manager has run THIS scheme >=3y (ideally 5y+ for a 10y horizon), or that the AMC runs a team/process mandate rather than a star-manager one — not scrapeable, engine blind.
- [ ] Full-holdings overlap: this fund's table is TRUNCATED (42/56 holdings, 85.76% of weight visible). Re-check this fund's overlap against the other picks on a tool with the COMPLETE holdings sheet before trusting the <=10% result.

## 2. Invesco India Mid Cap Fund Direct Plan Growth Option  (score 75.757937)
*Invesco_Asset_Management_India_Private_Ltd — Mid-Cap — growth bucket*

growth pick (Mid-Cap); 10Y alpha 2.98 vs category -0.57 (+3.55 excess); alpha stability: worst-horizon excess +3.55 and positive in 3/3 horizons; Sharpe 0.79 vs 0.6 (+0.19) (clears the risk-free hurdle); beta 0.89; captures 97.0 of up-markets vs 85.0 of down-markets (spread +12.0); max drawdown -25.77% vs category -33.61% (shallower); recovered from max drawdown in 2 months; holdings avg star 2.794177, top-10 = 47% of assets, turnover 48.24%

**Manual verification before investing:**
- [ ] Sortino ratio: confirm top-quartile vs category (Value Research / Rupeevest / AMC factsheet) — engine uses Sharpe + downside-capture only; Sortino is not on Morningstar's public pages.
- [ ] Manager tenure: confirm the lead manager has run THIS scheme >=3y (ideally 5y+ for a 10y horizon), or that the AMC runs a team/process mandate rather than a star-manager one — not scrapeable, engine blind.

## 3. Axis Small Cap Fund Direct Growth  (score 80.051587)
*Axis_Asset_Management_Company_Limited — Small-Cap — aggressive bucket*

aggressive pick (Small-Cap); 10Y alpha 5.82 vs category 2.86 (+2.96 excess); alpha stability: worst-horizon excess +0.4 and positive in 3/3 horizons; Sharpe 0.79 vs 0.61 (+0.18) (clears the risk-free hurdle); beta 0.71; captures 80.0 of up-markets vs 55.0 of down-markets (spread +25.0); max drawdown -30.2% vs category -41.53% (shallower); recovered from max drawdown in 2 months; holdings avg star 2.853422, top-10 = 19% of assets, turnover 39.98%

**Manual verification before investing:**
- [ ] Sortino ratio: confirm top-quartile vs category (Value Research / Rupeevest / AMC factsheet) — engine uses Sharpe + downside-capture only; Sortino is not on Morningstar's public pages.
- [ ] Manager tenure: confirm the lead manager has run THIS scheme >=3y (ideally 5y+ for a 10y horizon), or that the AMC runs a team/process mandate rather than a star-manager one — not scrapeable, engine blind.
- [ ] Full-holdings overlap: this fund's table is TRUNCATED (95/133 holdings, 79.48% of weight visible). Re-check this fund's overlap against the other picks on a tool with the COMPLETE holdings sheet before trusting the <=10% result.

### Bench — pre-validated substitutes
_If a pick fails post-engine verification (e.g. the Stage 3 NAV rolling-return check), these same-bucket funds already clear every selection constraint against the remaining picks. Stage 3 verifies them alongside the picks. For a full re-selection instead, re-run with `--exclude 'failed fund name'`._
- for **Parag Parikh Flexi Cap Direct Growth** (core): Mahindra Manulife Focused Fund Direct Growth (score 81.619048, Mahindra_Manulife_Investment_Management_Pvt_Ltd); WhiteOak Capital Large Cap Fund Direct Growth (score 75.484127, WhiteOak_Capital_Asset_Management_Limited)
- for **Invesco India Mid Cap Fund Direct Plan Growth Option** (growth): Nippon India Growth Mid Cap Fund - Direct Plan Bonus Plan - Bonus (score 64.488095, Nippon_Life_India_Asset_Management_Ltd); Nippon India Growth Mid Cap Fund - Direct Plan - Growth Mid Cap (score 64.448413, Nippon_Life_India_Asset_Management_Ltd)
- for **Axis Small Cap Fund Direct Growth** (aggressive): Nippon India Small Cap Fund - Direct Plan - Growth Plan (score 65.952381, Nippon_Life_India_Asset_Management_Ltd); Edelweiss Small Cap Fund Direct Growth (score 63.27381, Edelweiss_Asset_Management_Limited)

### Pairwise equity overlap (%)  —  measured (worst-case)
- Invesco India Mid Cap Fund Direct Plan Growth Option x Axis Small Cap Fund Direct Growth: 9.47  (worst-case 29.99)
- Parag Parikh Flexi Cap Direct Growth x Axis Small Cap Fund Direct Growth: 2.66  (worst-case 37.42)
- Parag Parikh Flexi Cap Direct Growth x Invesco India Mid Cap Fund Direct Plan Growth Option: 0.04  (worst-case 14.28)

_Holdings coverage (scraped / declared, % weight visible):_
- Axis Small Cap Fund Direct Growth: 95/133 holdings, 79.48% weight  ⚠ TRUNCATED
- Invesco India Mid Cap Fund Direct Plan Growth Option: 42/42 holdings, 98.58% weight
- Parag Parikh Flexi Cap Direct Growth: 42/56 holdings, 85.76% weight  ⚠ TRUNCATED

### ⚠ Overlap could not be certified — VERIFY EXTERNALLY
_These pairs pass the overlap gate on SCRAPED holdings, but a truncated table means the TRUE overlap could exceed the 10.0% budget. Re-check on a tool with the complete holdings sheets before investing._
- Parag Parikh Flexi Cap Direct Growth x Invesco India Mid Cap Fund Direct Plan Growth Option: measured 0.04% but worst-case up to 14.28% (budget 10.0%)
- Parag Parikh Flexi Cap Direct Growth x Axis Small Cap Fund Direct Growth: measured 2.66% but worst-case up to 37.42% (budget 10.0%)
- Invesco India Mid Cap Fund Direct Plan Growth Option x Axis Small Cap Fund Direct Growth: measured 9.47% but worst-case up to 29.99% (budget 10.0%)

### Near-miss watchlist (diagnostic — never selected)
_Funds that failed the fewest gates, closest miss first. The gate is NOT relaxed for them — this only shows how narrowly each was rejected._
- Aditya Birla Sun Life Focused Fund Direct Plan Growth (Focused Fund, core): failed `alpha_vs_category` — value -0.01 vs threshold 0.0 (margin -0.01)
- Sundaram Balanced Advantage Fund - Direct Plan - Growth Option (Dynamic Asset Allocation, diversifier): failed `sharpe_vs_category` — value -0.01 vs threshold 0.0 (margin -0.01)
- WhiteOak Capital Balanced Advantage Fund Direct Growth (Dynamic Asset Allocation, diversifier): failed `beta_within_bucket_band` — value 1.07 vs threshold 1.05 (margin -0.02)
- HSBC Flexi Cap Fund Growth Direct (Flexi Cap, core): failed `beta_within_bucket_band` — value 1.03 vs threshold 1.0 (margin -0.03)
- Navi Aggressive Hybrid Fund Direct Growth (Aggressive Allocation, diversifier): failed `beta_within_bucket_band` — value 1.09 vs threshold 1.05 (margin -0.04)
- UTI Large cap Fund Growth Option - Direct (Large-Cap, core): failed `alpha_vs_category` — value -0.04 vs threshold 0.0 (margin -0.04)
- Groww Large Cap Fund Direct Plan Growth Option (Large-Cap, core): failed `downside_capture_vs_category` — value 104.0 vs threshold 103.95 (margin -0.05)
- Invesco India Flexi Cap Fund Direct Growth (Flexi Cap, core): failed `beta_within_bucket_band` — value 1.05 vs threshold 1.0 (margin -0.05)
- Edelweiss Aggressive Hybrid Direct Plan Growth Option (Aggressive Allocation, diversifier): failed `beta_within_bucket_band` — value 1.11 vs threshold 1.05 (margin -0.06)
- Mahindra Manulife Aggressive Hybrid Fund Direct Growth (Aggressive Allocation, diversifier): failed `beta_within_bucket_band` — value 1.11 vs threshold 1.05 (margin -0.06)
- Mirae Asset Aggressive Hybrid Fund -Direct Plan-Growth (Aggressive Allocation, diversifier): failed `beta_within_bucket_band` — value 1.11 vs threshold 1.05 (margin -0.06)
- Bandhan Aggressive Hybrid Fund Direct Plan Growth (Aggressive Allocation, diversifier): failed `beta_within_bucket_band` — value 1.12 vs threshold 1.05 (margin -0.07)
- Quant Aggressive Hybrid Fund Growth Option Direct Plan (Aggressive Allocation, diversifier): failed `beta_within_bucket_band` — value 1.15 vs threshold 1.05 (margin -0.1)
- Mahindra Manulife Balanced Advantage Fund Direct Growth (Dynamic Asset Allocation, diversifier): failed `beta_within_bucket_band` — value 1.19 vs threshold 1.05 (margin -0.14)
- Bandhan Flexi Cap Fund-Direct Plan-Growth (Flexi Cap, core): failed `alpha_vs_category` — value -0.15 vs threshold 0.0 (margin -0.15)
- …and 25 more (full list in recommendations.json `near_misses`)

### Caveats
- 1798 enriched funds loaded; 380 in universe after plan filter (Direct+Growth) and bucket mapping.
- 2 fund(s) have NAV dates older than the snapshot max (2026-07-06) — pricing may be stale: ['Kotak India Growth Fund Series 4 Direct Dividend option Income Dis cum Cap wdrl', 'Kotak India Growth Fund Series 4 Direct Growth']
- horizon preference ['10Y', '5Y', '3Y'] derived from investment_horizon_years=10.
- near_misses is a DIAGNOSTIC watchlist of funds that failed a small number of gates by the recorded margin — they are never scored or selected; gates are never relaxed to admit them.
- bench lists pre-validated substitutes per pick (same bucket, no AMC/category/overlap conflict with the OTHER picks) — reporting only; use one when a pick fails the Stage 3 NAV check, or re-run with --exclude for a full re-selection.
- Metrics are Morningstar's published risk tables for the scraped snapshot; they change as the site updates — the input_hashes pin exactly which snapshot produced this report.
- Overlap uses scraped equity-holding NAMES (Morningstar's public table may list fewer rows than the fund's full portfolio), so overlap_matrix_pct is a LOWER bound; overlap_upper_bound_pct is the rigorous worst case and overlap_uncertain_pairs flags any pair that cannot be certified within budget from scraped data.
- Past performance does not predict future returns; mutual fund investments are subject to market risk.
- Determinism guarantee: identical config_hash + input_hashes -> identical run_hash. If a re-run differs, diff the hashes first.
