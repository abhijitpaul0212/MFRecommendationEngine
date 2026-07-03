# MFRecommendationEngine

Here is a 4-step framework you can run periodically to identify 3-4 high-performing funds with high confidence and controlled risk.Step 1: Asset Allocation (The Structural Foundation)To ensure your portfolio has less than 10% overlap, you cannot pick funds from the same category or with the identical investment styles. For a 3-4 fund portfolio over a 10-year horizon, structure your core and satellite buckets first:Fund 1 (Core Anchor): Flexi Cap or Large & Mid Cap (Focus: Stability and steady compounding).Fund 2 (Growth Satellite): Mid Cap Fund (Focus: Alpha generation).Fund 3 (Aggressive Satellite): Small Cap Fund (Focus: Long-term explosive growth).Fund 4 (Hedge/Diversifier): Value Fund, Multi-Asset, or International Index (Focus: Low correlation to the broader Indian growth market).Step 2: The Quantitative Filter (Risk & Alpha)This is where your technical indicators come in. Run a fund screener with these exact parameters to filter the universe of direct funds:Alpha (Excess Return): Look for an Alpha of > 2.0. More importantly, the Alpha must be positive against both the benchmark index and the category average. This proves the fund manager has stock-picking skill rather than just riding a bull market.Calculation: $\alpha = R_p - [R_f + \beta (R_m - R_f)]$Sharpe Ratio (Risk-Adjusted Return): This measures how much excess return you receive for the volatility you are enduring. Aim for a Sharpe Ratio > 1.5, or firmly in the top quartile of the category.Calculation: $S = \frac{R_p - R_f}{\sigma_p}$Sortino Ratio (Downside Protection): Often more critical than Sharpe, Sortino only penalizes downward volatility. A higher Sortino ratio indicates the fund protects your capital better during market corrections.Beta (Market Sensitivity): * Core Fund: Aim for a Beta of ~0.85 to 0.95 (meaning it falls less than the market during crashes).Satellite Funds: A Beta of 1.0 to 1.15 is acceptable only if the Alpha is exceptionally high to compensate for the extra volatility.Step 3: The Overlap Test (True Diversification)Once you have top funds from Step 2, run them through a portfolio overlap tool.The Problem: If you buy a Flexi Cap and a Large Cap fund, you might be buying HDFC Bank and Reliance twice, paying two expense ratios for the exact same underlying assets.The Rule: Compare Fund 1 vs Fund 2, Fund 1 vs 3, etc. Reject any fund that shares more than 10% of its underlying stock portfolio with another fund in your selection.Pro-Tip: Choosing funds from different Asset Management Companies (AMCs) and contrasting investment styles (e.g., pairing a 'Growth' AMC with a 'Value' AMC) naturally reduces overlap.Step 4: The Repeatability & Authenticity CheckTo ensure this framework yields the same level of confidence every time you run it:Use Rolling Returns: Never look at point-to-point "1-year" or "3-year" returns. Look at 3-year and 5-year rolling returns. A fund should beat its benchmark >70% of the time on a rolling basis. This removes the luck of market timing.Manager Consistency: Ensure the current fund manager has been at the helm for at least 3 to 5 years. A fund's historical Alpha belongs to the manager, not the AMC. If the manager leaves, the framework requires you to re-evaluate the fund.

## End-to-end runbook (plain Python — no Claude required)

The entire pipeline is standalone Python. Claude skills are OPTIONAL wrappers
that run these exact same commands — in production you can run everything
below directly and pay zero model tokens.

```bash
# STEP 0 — one-time setup (idempotent)
./setup.sh && source .venv/bin/activate
python -m pytest tests/ -q                        # all suites must pass

# STEP 1 — SCRAPE (one canonical script, three modes; list + enrichment in one run)
# (a) one fund house (full report: list + every fund enriched):
python scraper/morningstar_fund_details.py --out ms_data --headless --workers 4 \
    --house "HDFC Asset Management Company Limited"
# (b) individual fund(s) within a house:
python scraper/morningstar_fund_details.py --out ms_data --headless \
    --house "Axis Asset Management Company Limited" \
    --fund "Axis Bluechip Fund Direct Plan Growth"
# (c) the FULL UNIVERSE (~47 houses, ~14k funds — hours even parallelised;
#     the script prints an estimate before enriching):
python scraper/morningstar_fund_details.py --out ms_data --headless --workers 8 --all
# House/fund names must match ms_data/filters.json EXACTLY. --limit N caps
# enrichment per house (testing aid). Re-runs refresh list data but PRESERVE
# previously enriched funds.

# STEP 2 — RECOMMENDATION ENGINE (no browser, no network; deterministic)
python selection/mf_recommend.py --selftest       # must print SELFTEST PASS
python selection/mf_recommend.py --data ms_data --out ms_data/recommendation_run
# -> recommendations.json (scores, gates, reasons, run_hash) + recommendations.md
```

Order matters: 1 → 2. The engine only sees funds the scraper enriched (it
needs the risk tables); re-run Step 2 alone any time to re-score the same
snapshot — identical run_hash proves nothing drifted.

### Claude skills ↔ python scripts mapping

| Claude skill (optional) | Runs exactly | When to prefer which |
|---|---|---|
| `/morningstar-scrape` | Step 1 above | skill: Claude supervises, validates output, retries failures; script: zero token cost, cron-able |
| `/mf-recommend` | Step 2 above + writes `model_judgment.md` | skill adds the model-judgment layer (interpretation, conviction, flags); script alone gives you everything deterministic |

**Production guidance:** once stable, schedule the three python steps directly
(cron/CI) — no Claude in the loop, no token spend. The single capability that
genuinely requires a model is `model_judgment.md` (qualitative inference over
`recommendations.json`); it is optional, additive, and can be invoked only
when you want the interpretive layer refreshed.

## Data acquisition: the single canonical scraper

`scraper/morningstar_fund_details.py` is the ONE scraping script — fund list +
per-fund detail enrichment consolidated. It feeds the Quantitative Filter
(Step 2) with everything scraped from
[morningstar.in](https://www.morningstar.in/default.aspx).

### Design architecture — three layers

1. **Pure core** (no selenium needed): `rows_to_json`, `is_next_disabled`,
   `normalize_scheme_name`, `attach_to_catalog`, `payload_hash`,
   `merge_house_into`, `atomic_write_json`, `derive_tab_urls`,
   `parse_star_rating`, `signed_share_change`, `nest_fund_details`,
   `merge_list_preserving_enrichment`. Unit-tested in
   `tests/test_morningstar_parse.py` + `tests/test_fund_details_parse.py` —
   runs in browserless CI.
2. **Page objects**: `FactsheetPage` (the WebForms list screen — user-supplied
   XPaths in `LOCATORS` with semantic fallbacks, ASP.NET `UpdatePanel` partial
   postbacks, `select2` widgets, layered popup dismissal) and `FundDetailPage`
   (the JS-rendered SAL detail pages — holdings summary, Equity/Bond holdings
   tables with pager, 3Y/5Y/10Y risk tables, iframe-tolerant waits).
3. **Orchestrator** (`MorningstarScraper`) — per house, one browser runs both
   phases:
   * **LIST**: select the house with Category/Distribution/Structure at their
     All-defaults, Go, 100 rows/page, paginate until
     `<a disabled="disabled">Next &gt;</a>` — collecting row data AND each
     fund's detail URL in a single pass.
   * **ENRICH**: per fund, open `detailed-portfolio.aspx` and
     `risk-ratings.aspx` (derived from the fund anchor — identical to clicking
     the tabs) and nest `detailed_portfolio` + `risk_ratings` under the fund.

### Three generation modes

```bash
./setup.sh && source .venv/bin/activate            # one-time setup
# one fund house | individual fund(s) | full universe:
python scraper/morningstar_fund_details.py --out ms_data --headless --workers 4 \
    --house "Axis Asset Management Company Limited"
python scraper/morningstar_fund_details.py --out ms_data --headless \
    --house "Axis Asset Management Company Limited" --fund "<exact fund name>"
python scraper/morningstar_fund_details.py --out ms_data --headless --workers 8 --all
python -m pytest tests/ -q                          # pure core, no browser
```

### Parallelism & safety model

`--workers N` runs N independent Chrome instances: houses are dealt
round-robin for the LIST phase, then all (house, fund, url) enrichment tasks
are dealt round-robin for the ENRICH phase — so even a single big house fans
out across all workers. Every merge into shared state happens under one lock
with atomic temp-file writes: per-house JSONs are always valid, a crash keeps
every completed fund, and per-house/per-fund failures are retried once then
recorded in the manifest (never silently dropped). **Re-runs refresh
list-level attributes but preserve previously enriched funds** (delisted funds
are dropped so the file mirrors the current snapshot).

Outputs in `ms_data/`: `filters.json` (all dropdown values), one
`<Fund_House>.json` per house (fund-name keys, same structure as always), and
`morningstar_factsheet.json` (combined manifest: `scraped_at`, per-house
counts, enriched count, failures, payload sha256 pinning the snapshot).

Note: Morningstar's public holdings table can display fewer rows than the
holdings-summary counts (e.g. 74 of 93 equity positions) — the scraper captures
exactly what the page displays.

> **Canonical pipeline:** `morningstar_fund_details.py` is the final, validated
> scraping script (field-level validation vs live pages: 90/90). Extend it;
> don't fork new scrapers.

## Recommendation engine — knowledge base

`selection/mf_recommend.py` (v1.1.0) turns the enriched snapshot into ranked,
explained recommendations. This section is the engine's knowledge base: every
attribute the scraper captures, how the engine uses it, and WHY — so future
optimization work starts from the reasoning, not just the code.

```bash
python selection/mf_recommend.py --selftest                       # SELFTEST PASS
python selection/mf_recommend.py --data ms_data --out ms_data/recommendation_run
```

### Data dictionary — every scraped attribute and its job

| Attribute (per fund JSON) | Used for | Why |
|---|---|---|
| `Category` | bucket mapping (core/growth/aggressive/diversifier) | Step 1 asset allocation: structural diversification precedes any fund merit |
| fund name | Direct+Growth plan filter | Regular/IDCW variants duplicate the same portfolio with worse economics for a self-directed long-horizon investor |
| `Latest NAV` / `NAV Date` | staleness note | a fund priced older than the snapshot max may carry stale metrics; flagged, never silently dropped |
| `risk_ratings.*.Alpha` (Inv, Cat) | gate + score + stability | manager skill is *excess* over category, not raw return; positive alpha vs peers is the entry ticket |
| `risk_ratings.*.Beta` (Inv) | bucket-specific band + compensation gate | core money must be defensive (β ≤ 1.00); satellites may run to 1.15 **only if** alpha excess ≥ 1.0 pays for the extra market risk |
| `risk_ratings.*.R-Squared` (Inv) | reliability gate (≥ 70) | α and β come from a regression; low R² means the benchmark explains little of the fund, so its α/β numbers are statistical noise — trusting them would be false precision |
| `risk_ratings.*.Sharpe Ratio` (Inv, Cat) | vs-category gate + **absolute floor** | Morningstar's Sharpe is already net of the risk-free rate, so `Sharpe ≥ 0` literally means "beat the risk-free return per unit of risk" — the risk-free hurdle the investor always has |
| `risk_ratings.*.Standard Deviation` (Inv, Cat) | volatility edge score | between two alpha-equal funds, the calmer one compounds better psychologically (investor stays invested) |
| `capture_ratios.Upside/Downside` (Inv, Cat) | best/worst-market behaviour: spread scored; downside capped absolutely (≤ 110) AND vs category (≤ 1.05×) | up/down capture is the cleanest scraped proxy for behaviour in best and worst market scenarios; the double cap keeps downside risk to a minimum both in absolute terms and relative to peers |
| `drawdown.Maximum` (Inv %, Cat %) | gate (≤ 1.10× category) + edge score | max drawdown is the realized worst case; a fund that falls much deeper than its own category adds risk the category label hides |
| `drawdown_dates.Max Duration` | recovery-speed score | two funds with equal −12% drawdowns are not equal if one took 5 months to recover and the other 2 years; time-under-water is the real cost of a crash to a goal-dated investor |
| `holdings.Equity[].% Portfolio Weight` | pairwise overlap between picks (≤ 10%) | Step 3: paying two expense ratios for the same stocks is diversification theatre |
| `holdings.Equity[].Equity Star Rating` | weight-averaged portfolio quality score | rates what the manager *owns right now*, complementing the return-based metrics which only rate what they owned in the past |
| `holdings.Equity[].Sector` | effective sector count (inverse-HHI), reported | intra-fund concentration context; cross-fund diversification is already enforced by category quotas |
| `holdings.Equity[].Share Change %` | captured (direction-signed) | churn signal; summarised better by Reported Turnover %, so not double-counted in scoring |
| `holdings_summary.% Assets in Top 10` | concentration score (lower better) | top-heavy funds carry idiosyncratic blow-up risk that σ/β understate |
| `holdings_summary.Reported Turnover %` | churn score (lower better) | high churn = transaction drag + style instability; long horizons reward patient managers |
| `holdings_summary.Equity/Bond/Total Holdings` | breadth, reported in metrics | context for concentration numbers |
| `risk_ratings` horizon keys (3Y/5Y/10Y) | horizon selection + alpha stability | see below |

### Horizon logic (investment-horizon aware)

`investment_horizon_years` (default 10) decides which risk tables **lead**:
≥ 10y → `10Y > 5Y > 3Y`; ≥ 5y → `5Y > 3Y`; else `3Y > 5Y`. Long money should
be judged on long windows; shorter windows remain fallbacks because 10Y tables
don't exist for younger funds (the report records `horizon_used` per fund).
**Nearest-complete-horizon rule (v1.2):** a horizon only counts as usable when
EVERY gate input is present (`REQUIRED_FOR_GATES`) — a missing category cell at
the lead horizon (e.g. Morningstar publishes no 10Y category drawdown for some
funds) makes the engine evaluate the fund at the next complete horizon instead
of failing it on a data gap. If no horizon is complete, the fund fails
`data_complete` — values are never guessed, gates never skipped piecemeal.
Independently of the lead horizon, **alpha stability** is judged across every
horizon available: `worst_alpha_excess` (the minimum alpha excess over
3Y/5Y/10Y) and `alpha_consistency` (how many horizons are positive). Rationale:
one lucky window is not skill; a manager whose *worst* horizon still beats the
category is the stability the framework wants.

### Gates (capital protection first — nothing scores past a failed gate)

| Gate | Default | Rationale |
|---|---|---|
| `data_complete` | required | missing metrics are never guessed |
| `alpha_vs_category` | excess > 0 | skill vs peers is the entry ticket |
| `sharpe_vs_category` | excess ≥ 0 | risk-adjusted edge vs peers |
| `sharpe_beats_risk_free` | Sharpe ≥ 0 | the absolute hurdle: beat risk-free per unit of risk |
| `r_squared_reliability` | R² ≥ 70 | below this, α/β are noise — reject rather than trust |
| `beta_within_bucket_band` | core 1.00 / growth+aggr 1.15 / divers 1.05 | Step-2 beta discipline per role |
| `high_beta_alpha_compensation` | β > 1.0 needs α-excess ≥ 1.0 | extra market risk must be paid for |
| `downside_capture_cap` | ≤ 110 absolute | worst-market hard ceiling |
| `downside_capture_vs_category` | ≤ 1.05× category | may not be a worse-than-peers loser |
| `drawdown_vs_category` | ≥ 1.10× category MDD | realized worst case within category norms |

### Scoring (weighted percentile ranks of gate survivors; weights sum to 100)

alpha_excess 20 · worst_alpha_excess 15 · sharpe_excess 15 · capture_spread 10
· downside_capture_low 10 · drawdown_edge 10 · drawdown_recovery_fast 5 ·
std_edge 5 · portfolio_quality 5 · concentration_low 3 · turnover_low 2.
Weighting philosophy: ~50% skill (alpha family + Sharpe), ~35% downside/
stability (capture, drawdown, recovery, volatility), ~15% portfolio hygiene
(quality, concentration, churn). Percentile ranks (not raw values) make weights
scale-free and robust to outliers; `None` ranks 0 — missing data never helps.

### Selection

Two passes (v1.2). **Structure first:** buckets are seated in
`bucket_priority` order (core → growth → aggressive → diversifier — README
Step 1: the portfolio's shape precedes raw score), best-scored eligible fund
per bucket, so a high-scoring satellite can no longer consume a shared
constraint (like the one-per-AMC slot) that the core anchor needed. **Fill
second:** best remaining by score from any bucket. Constraints throughout:
max 1 per AMC, max 1 per category, pairwise scraped-holdings overlap ≤ 10%.
Every pick carries a `recommendation_reason` assembled from the same numbers
that drove its gates and score — text can never drift from arithmetic. An
**empty or short recommendation list is a valid outcome** (nothing passed the
gates, or every remaining candidate was blocked by a real constraint — an
unfilled slot beats diversification theatre).

### Determinism rules (apply to any future change)

No RNG; no wall-clock inside the hashed payload (`generated_at` recorded but
excluded from `run_hash`); floats through `r6()` before comparison; every
ordering ends in a fund-name tie-break; config and inputs hashed into the
manifest so identical `(snapshot, config)` provably yields identical output.

### Model-judgment layer (on top of the deterministic core)

The engine's scores are deterministic; **interpretation is a model task**.
After every engine run, Claude (via the `/mf-recommend` skill) produces
`model_judgment.md` next to `recommendations.json`, following this contract:

1. **Binding** — the judgment opens with the `run_hash` it interprets; a
   judgment is only valid for that exact deterministic run.
2. **Grounding** — every quantitative claim must cite a number present in
   `recommendations.json` (metrics, gate checks, scores, overlap). No outside
   data, no recalled fund folklore, no invented numbers.
3. **Non-interference** — the judgment may NOT alter scores, picks or gates.
   Where the model disagrees with an outcome, it records a **model flag**
   (e.g. "gate X may be too strict for hybrid categories") — flags are input
   for future config tuning, not overrides.
4. **Coverage per recommended fund** — inference on manager skill (alpha family),
   horizon fit (which tables led and what that implies), the risk-free hurdle
   (Sharpe context), alpha stability across best/worst market scenarios
   (worst-horizon excess, capture ratios), downside profile (captures,
   drawdown depth + recovery), holdings hygiene (quality/concentration/churn)
   — ending in a stated conviction (High / Medium / Low) with its basis.
5. **Portfolio-level synthesis** — bucket coverage, pairwise overlap, combined
   risk posture, and what would change the view (falsifiers).

Division of labour: **deterministic where required** (screening, scoring,
selection — reproducible, hash-provable), **model judgment where valuable**
(inference, conviction, caveats — grounded but not hash-deterministic).

### Version history / future hooks

- **v1.0.0** — gates (α, Sharpe, flat β, downside vs cat, drawdown), 6-factor score.
- **v1.1.0** — horizon-aware lead tables; risk-free Sharpe floor; R² reliability
  gate; bucket beta bands + high-β alpha compensation; absolute downside cap;
  alpha stability (worst-horizon excess); recovery-duration, portfolio-quality,
  concentration and turnover scoring; NAV staleness notes; sector effective-N.
- **v1.2.0** — implemented both model flags from the v1.1 judgment:
  (1) nearest-complete-horizon rule (`REQUIRED_FOR_GATES`): a data gap at the
  lead horizon falls back to the next complete horizon instead of failing the
  fund (this seated Parag Parikh Flexi Cap, previously excluded because
  Morningstar publishes no 10Y category drawdown for it); (2) bucket-priority
  structural selection (core seated first) so satellites can't consume the
  core anchor's AMC slot. Verified: portfolio restructured with a core anchor,
  change fully attributable to the engine/config hash.
- Open model flag (v1.2 judgment): fixed bucket order can still seat a weaker
  satellite pair when two candidates share an AMC — consider solving the small
  bucket-assignment problem (maximise total score subject to quotas/AMC/
  category/overlap) instead of a fixed priority order.
- Unused-but-captured (candidates for future versions): per-holding
  `Share Change %` trend, `First Bought` dates (not scraped), bond-holding
  credit quality (not published in the scraped table), Index columns of the
  risk tables (mostly em-dash on Morningstar India).

## Claude skills

Two project skills automate the workflow end-to-end (see
[.claude/skills/README.md](.claude/skills/README.md)): `/morningstar-scrape`
refreshes the data snapshot; `/mf-recommend` re-runs the engine on it —
designed so a future-dated re-run is reproducible and any change in output is
attributable to data (`input_hashes`) or rules (`config_hash`), never chance. A live site is a changing input, so run-to-run identity is not
promised — but the manifest hash lets any downstream framework run pin exactly which
snapshot it consumed. **Be polite**: keep the conservative defaults (single session,
sequential requests, `ACTION_DELAY` between interactions) and check Morningstar's
Terms of Use and robots.txt before running.