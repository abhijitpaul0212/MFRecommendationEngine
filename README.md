# MFRecommendationEngine

## Table of contents

- [Quick guide: end-to-end in four commands (+ periodic rebalance)](#quick-guide-end-to-end-in-four-commands--periodic-rebalance)
  - [Skills & agents (optional Claude wrappers)](#skills--agents-optional-claude-wrappers-over-the-same-clis)
- [The founding framework (manual process this repo automates)](#the-founding-framework-manual-process-this-repo-automates)
- [End-to-end runbook (plain Python — no Claude required)](#end-to-end-runbook-plain-python--no-claude-required)
  - [Claude skills ↔ python scripts mapping](#claude-skills--python-scripts-mapping)
- [Data acquisition: the single canonical scraper](#data-acquisition-the-single-canonical-scraper)
  - [Design architecture — three layers](#design-architecture--three-layers)
  - [Three generation modes](#three-generation-modes)
  - [Parallelism & safety model](#parallelism--safety-model)
  - [Resource governor & watchdog (laptop-safe scraping)](#resource-governor--watchdog-laptop-safe-scraping)
  - [Data-completeness model (validate → audit → repair)](#data-completeness-model-validate--audit--repair)
- [Recommendation engine — knowledge base](#recommendation-engine--knowledge-base)
  - [Data dictionary — every scraped attribute and its job](#data-dictionary--every-scraped-attribute-and-its-job)
  - [Attribute audit — scraped vs consumed](#attribute-audit--scraped-vs-consumed-verified-against-the-2532-fund-snapshot)
  - [Horizon logic (investment-horizon aware)](#horizon-logic-investment-horizon-aware)
  - [Universe filter (which funds enter scoring at all)](#universe-filter-which-funds-enter-scoring-at-all)
  - [Gates (capital protection first)](#gates-capital-protection-first--nothing-scores-past-a-failed-gate)
  - [Selection constraints](#selection-constraints-applied-to-gate-survivors-after-scoring)
  - [Scoring](#scoring-weighted-percentile-ranks-of-gate-survivors-weights-sum-to-100)
  - [Selection](#selection)
  - [Bench & closed-loop exclusions](#bench--closed-loop-exclusions-v14)
  - [Near-miss watchlist](#near-miss-watchlist-v13--visibility-not-relaxation)
  - [Bucket-role note: the diversifier beta band and hybrids](#bucket-role-note-the-diversifier-beta-band-and-hybrids)
  - [Determinism rules](#determinism-rules-apply-to-any-future-change)
  - [Longitudinal history](#longitudinal-history-ms_datametrics_historyjsonl)
  - [Model-judgment layer](#model-judgment-layer-on-top-of-the-deterministic-core)
  - [Version history / future hooks](#version-history--future-hooks)
- [Post-engine verification: NAV rolling-return check](#post-engine-verification-nav-rolling-return-check-nav_rolling_checkpy)
- [Allocation planner: amount / risk / duration → exact breakdown](#allocation-planner-amount--risk--duration--exact-breakdown-mf_allocatepy)
- [Rebalancing audit: drift + full quality re-check](#rebalancing-audit-drift--full-quality-re-check-mf_rebalancepy)
- [Dormant alternate engine: NAV-based deterministic selection](#dormant-alternate-engine-nav-based-deterministic-selection-mf_selectpy)
- [Claude skills & agents](#claude-skills--agents)

## Quick guide: end-to-end in four commands (+ periodic rebalance)

The initial investment is four commands, run in order; Stage 5 is the
periodic maintenance audit you re-run months later. Full argument reference
for each is below; deeper rationale lives in the linked sections.

```bash
source .venv/bin/activate                          # once per shell (./setup.sh first time)

# 1. SCRAPE the universe (hours; resumable — re-runs preserve enriched funds)
python scraper/morningstar_fund_details.py --out ms_data --headless --workers 2 \
    --all --direct-growth-only --equity-holdings-only \
    --recommendation-universe-only --refresh-days 30

# 2. RECOMMEND from the snapshot (seconds; deterministic, offline)
python selection/mf_recommend.py --data ms_data --out recommendation_run

# 3. VERIFY finalists against full NAV history (seconds; AMFI data, no browser)
python selection/nav_rolling_check.py --report recommendation_run/recommendations.json

# 4. ALLOCATE — your amount / risk / duration -> exact % + ₹ per fund
python selection/mf_allocate.py --report recommendation_run/recommendations.json \
    --amount 1000000 --risk moderate --years 15   # omit flags to be prompted

# 5. LATER, PERIODICALLY (e.g. annually) — REBALANCING AUDIT: re-run steps
#    1-2 for a fresh snapshot first, then audit the live portfolio against
#    the buy-time plan (drift) AND today's full quality machinery
python selection/mf_rebalance.py --plan recommendation_run/allocation_plan.json \
    --report recommendation_run/recommendations.json --data ms_data
```

Then read `recommendation_run/recommendations.md` — picks, scores,
reasons, the **bench** (pre-validated substitutes per pick), overlap matrix
(with worst-case bounds), the per-fund **manual verification checklist**
(Sortino top-quartile, manager tenure) — and the Stage 3 console verdicts.
Step 3 verifies the bench alongside the picks, so a pick `FAIL` usually has
a **pre-verified substitute** already sitting next to it. If the bench can't
cover it (or you prefer a full constraint-checked rebuild), close the loop:

```bash
python selection/mf_recommend.py --data ms_data --out recommendation_run \
    --exclude "the failed fund's exact name"     # repeatable
# then re-run step 3 on the new report
```

### 1. Scraper — all arguments (`scraper/morningstar_fund_details.py`)

| Argument | Default | What it does |
|---|---|---|
| `--all` \| `--house NAME` | (one required) | full universe (~47 houses), or one house (repeatable; exact name from `ms_data/filters.json`) |
| `--fund NAME` | — | restrict enrichment to specific fund(s) within `--house` (repeatable, exact name) |
| `--out DIR` | `ms_data` | output dir: per-house JSONs + manifest + filters |
| `--headless` | off | run Chrome headless (recommended for full runs) |
| `--workers N` | `4` | parallel browsers; clamped by CPU ceiling AND startup RAM sizing (~1.2 GB/worker) — asking for more than RAM sustains does not go faster |
| `--force-workers` | off | bypass both worker clamps (expect renderer timeouts) |
| `--direct-growth-only` | off | enrich only Direct+Growth plan variants — the only ones the engine consumes (~75% less work) |
| `--equity-holdings-only` | off | skip the Bond-holdings pager walk (engine reads Equity rows + summary only) |
| `--recommendation-universe-only` | off | enrich only the ~14 categories the engine scores; skips ~80% of funds (debt/index/sector/long-short) it never selects |
| `--refresh-days N` | off (always re-enrich) | skip funds already enriched within N days (risk/holdings update ~monthly; add once you have a baseline) |
| `--limit N` | — | cap enriched funds per house (testing aid) |
| `--delay SEC` | `2.0` | polite pause between page interactions (floor 1.0) |

List-level data (NAV, category) is always captured for every fund regardless
of the enrichment-narrowing flags — they only skip the expensive per-fund
detail pages. See [Data acquisition](#data-acquisition-the-single-canonical-scraper).

### 2. Recommendation engine — all arguments (`selection/mf_recommend.py`)

| Argument | Default | What it does |
|---|---|---|
| `--data DIR` | `ms_data` | dir of enriched per-house JSON files (Stage 1 output) |
| `--out DIR` | `recommendation_run` | writes `recommendations.json` + `recommendations.md` |
| `--config FILE` | — | JSON overrides merged onto `DEFAULT_CONFIG` (gates, weights, quotas, horizon) |
| `--exclude FUND` | — | bar this fund from selection (repeatable, exact name) — still ranked, decision logged as `excluded_by_config`, hashed into `config_hash`. Use after a Stage 3 `FAIL` to rebuild the portfolio under full constraints |
| `--no-history` | off | skip appending this run to `<data>/metrics_history.jsonl` |
| `--selftest` | — | run the built-in deterministic selftest and exit (no data needed) |

No browser, no network; same snapshot + same config ⇒ identical `run_hash`.
See [Recommendation engine — knowledge base](#recommendation-engine--knowledge-base).

### 3. NAV rolling-return check — all arguments (`selection/nav_rolling_check.py`)

| Argument | Default | What it does |
|---|---|---|
| `--report FILE` \| `--fund NAME` | (one required) | check every pick in a `recommendations.json` **plus its bench substitutes**, or named fund(s) directly (repeatable) |
| `--no-bench` | off | with `--report`: check only the picks, skip the bench substitutes |
| `--map 'Fund Name=schemeCode'` | — | pin a fund to an AMFI scheme code when name matching is ambiguous (repeatable) |
| `--out DIR` | report's dir (CWD with `--fund`) | where `nav_rolling_check.json` is written |

Verdicts: `PASS` / `FAIL` / `SHORT_HISTORY` (genuinely young fund) /
`INCOMPLETE_HISTORY` (data looks partial or stale) / `UNRESOLVED`; exit code
`0` only if all **picks** pass — bench verdicts are contingency information
and never fail the run. See
[Post-engine verification](#post-engine-verification-nav-rolling-return-check-nav_rolling_checkpy).

### 4. Allocation planner — all arguments (`selection/mf_allocate.py`)

| Argument | Default | What it does |
|---|---|---|
| `--report FILE` | (required) | `recommendations.json` from Stage 2 |
| `--frequency` | `lumpsum` | `lumpsum` (one-time) or `sip` (recurring monthly) — the bucket-weighting math is identical either way; only the amount's meaning and the recorded schedule differ |
| `--amount N` | prompted | lumpsum total, OR the monthly SIP installment amount when `--frequency sip`, whole rupees |
| `--risk PROFILE` | prompted | `conservative` \| `moderate` \| `aggressive` |
| `--years N` | prompted | intended holding duration (refused under 5y — all-equity is wrong for short money) |
| `--sip-day N` | prompted (SIP only) | recurring debit day of month, 1–28 (avoids short-month ambiguity) |
| `--start-date YYYY-MM-DD` | today (SIP only) | first installment date — recorded in the plan so Stage 5 can reconstruct every installment |
| `--nav-check FILE` | auto-detected next to the report | Stage 3 verdicts; a pick that FAILED **blocks** the plan |
| `--step N` | `1000` | amount granularity in rupees — every figure is a multiple of this (order-friendly); `--step 1` for rupee-exact splits |
| `--allow-failed` | off | downgrade the Stage 3 FAIL block to a recorded warning |
| `--out DIR` | report's dir | writes `allocation_plan.json` + `allocation_plan.md` |

SIP is opt-in: `--frequency` defaults to `lumpsum`, so existing commands and
automation are unaffected. Passing `--frequency sip` without `--sip-day`/
`--start-date` prompts for them; passing them with `--frequency lumpsum` is
a usage error (they'd be silently ignored otherwise).

Weights come from an explicit risk × horizon template table (9 rows, each
summing to 100); unfilled buckets redistribute proportionally (warned, never
silent). The split is PRACTICAL: whole-number percentages (always summing to
exactly 100) and round amounts in `--step` multiples — e.g. 44% / ₹440,000
rather than 43.75% / ₹437,500 — while the whole amount is still invested (a
non-round total's sub-step residue goes to the largest allocation, noted in
the plan). See
[Allocation planner](#allocation-planner-amount--risk--duration--exact-breakdown-mf_allocatepy).

### 5. Rebalancing audit — all arguments (`selection/mf_rebalance.py`)

| Argument | Default | What it does |
|---|---|---|
| `--plan FILE` | (required) | `allocation_plan.json` from Stage 4 — the buy-time contract, including its recorded `frequency` (lumpsum/sip) and SIP schedule |
| `--report FILE` | (required) | **FRESH** `recommendations.json` (re-run Stages 1–2 first — quality is judged on today's snapshot) |
| `--data DIR` | `ms_data` | fresh scrape dir, for the held-fund overlap re-check |
| `--buy-date YYYY-MM-DD` | read from the plan | anchor date override: the lumpsum buy date, OR the SIP first-installment date |
| `--sip-day N` | read from the plan | SIP debit-day override (ignored for lumpsum plans) |
| `--as-of YYYY-MM-DD` | today | audit as of this date — also the last date SIP installments are counted through |
| `--current 'Fund=rupees'` | — | exact current value override (repeatable, highest priority) — use when you know the value better than any derivation |
| `--transactions FILE` | — | JSON `{fund: [{"date":..,"amount":..}, ...]}` of ACTUAL purchase dates/amounts — the precise fix for an irregular SIP (missed/changed installments) or a lumpsum top-up mixed into a SIP |
| `--new-money N` | `0` | rebalance by adding fresh cash; large enough N gives a pure-buy (no-sell, tax-friendlier) plan |
| `--drift-pp N` / `--drift-rel N` | `5` / `25` | the 5/25 drift rule: breach when off target by >N pp absolute OR >N% of target |
| `--map 'Fund Name=schemeCode'` | — | pin AMFI scheme codes (as in Stage 3) |
| `--skip-rolling` | off | skip the live rolling re-check (faster; recorded as a warning) |
| `--out DIR` | plan's dir | writes `rebalance_plan.json` + `rebalance_plan.md` |

Current value is derived **frequency-aware**, straight from the plan's own
contract: lumpsum replays a single buy date; SIP replays every monthly
installment from `sip_start_date`/`sip_day` through `--as-of`, each priced
at *that installment's own* historical NAV (never one NAV for the whole
position — that's what the old single-date formula got wrong for a SIP).
Priority per fund: `--current` (exact) > `--transactions` (dated purchases,
for irregular SIPs or mixed top-ups) > the plan's automatic schedule.

Verdicts: `HOLD` (exit 0) / `REBALANCE_REQUIRED` (exact buy/sell trades;
SIP portfolios also get a note suggesting adjusting the *next installment's*
split instead) / `REPLACEMENT_REQUIRED` (a held fund fails today's gates or
rolling check — composition first, trades never computed) / `INCONCLUSIVE`
(missing data blocks trades). Every verdict ships with its numbers and
reasoning. See
[Rebalancing audit](#rebalancing-audit-drift--full-quality-re-check-mf_rebalancepy).

### Skills & agents (optional Claude wrappers over the same CLIs)

Every stage above is plain Python — cron-able, zero token cost. For
Claude-supervised runs the repo ships project skills and an agent
(details: [.claude/skills/README.md](.claude/skills/README.md)):

| Wrapper | Drives | Adds on top of the bare CLI |
|---|---|---|
| `/morningstar-scrape` skill | Stage 1 | supervises the run, validates manifest/output, retries failures |
| `/mf-recommend` skill | Stage 2 | writes the `model_judgment.md` interpretation layer |
| **`mf-portfolio-loop` agent** | Stages 2→3→4 as a closed loop | on a Stage 3 pick FAIL, feeds the fund back via `--exclude` and rebuilds until every pick passes, then allocates (lumpsum or SIP); encodes the structural selection heuristics (blended core collapses the growth bucket; diversifier is unfillable next to a large-cap core; UNRESOLVED = retry) and cites the `run_hash → nav verdicts → plan_hash` chain in its conclusion |

The agent never hand-patches picks or plans — every action it takes is one of
the deterministic CLIs above, so its conclusions inherit the same
reproducibility contract. Stage 5 stays a direct CLI run.

## The founding framework (manual process this repo automates)

A 4-step process to identify 3–4 high-performing funds with high confidence
and controlled risk, run periodically. This is the conceptual origin of the
buckets, overlap rule and gates below — "Framework Step N" elsewhere in this
document refers back to these four steps (distinct from the pipeline's own
numbered run steps in the next section).

**Framework Step 1 — Asset Allocation (the structural foundation).** To keep
portfolio overlap under 10%, don't pick funds from the same category or
identical investment styles. For a 3–4 fund, 10-year-horizon portfolio,
structure core and satellite buckets first:
- **Fund 1 (Core Anchor)** — Flexi Cap or Large & Mid Cap: stability and steady compounding.
- **Fund 2 (Growth Satellite)** — Mid Cap: alpha generation.
- **Fund 3 (Aggressive Satellite)** — Small Cap: long-term explosive growth.
- **Fund 4 (Hedge/Diversifier)** — Value, Multi-Asset, or International Index: low correlation to the broader Indian growth market.

**Framework Step 2 — the quantitative filter (risk & alpha).** Screen the
direct-fund universe on:
- **Alpha** — positive against *both* the benchmark index and the category
  average, proving stock-picking skill rather than a bull-market ride:
  $\alpha = R_p - [R_f + \beta (R_m - R_f)]$
- **Sharpe Ratio** — reward per unit of volatility, ideally top-quartile of category: $S = \frac{R_p - R_f}{\sigma_p}$
- **Sortino Ratio** — like Sharpe but penalizes only downside volatility; often the more decision-relevant of the two for capital protection.
- **Beta** — core funds should fall less than the market in a crash
  (β meaningfully below 1); satellites may run hotter (β up to ~1.15) *only*
  if alpha is exceptional enough to compensate for the added volatility.

**Framework Step 3 — the overlap test (true diversification).** Run the
shortlist through a portfolio overlap tool. The problem: a Flexi Cap and a
Large Cap fund may both hold HDFC Bank and Reliance — two expense ratios for
the same bet. The rule: compare every pair; reject any pair sharing more than
10% of its underlying holdings. Pro-tip: different AMCs and contrasting
styles (pairing a "Growth" house with a "Value" house) naturally reduce overlap.

**Framework Step 4 — the repeatability & authenticity check.** To keep
confidence consistent run over run: use **rolling** 3-year/5-year returns
(never point-to-point "1-year" figures) — a fund should beat its benchmark on
a rolling basis more than 70% of the time, which removes market-timing luck.
Also check **manager consistency**: alpha belongs to the manager, not the
AMC, so a fund needs the current manager to have been at the helm 3–5+ years;
a manager departure means re-evaluating the fund from scratch.

> **Reconciliation — where the built engine intentionally diverges.** The
> deterministic engine ([selection/mf_recommend.py](selection/mf_recommend.py),
> knowledge base below) implements Steps 1 and 3 essentially as written
> (buckets, ≤10% overlap, one fund per AMC). Step 2 is implemented with
> **relative, category-aware gates** instead of the fixed absolute bars above
> (alpha excess *vs category* > 0 rather than alpha > 2.0 absolute; Sharpe
> *vs category* plus a risk-free floor rather than a flat 1.5) — Morningstar's
> published tables make peer-relative comparison the more honest signal than
> a hardcoded threshold that drifts with market regime. **Sortino Ratio and
> manager-tenure are not implemented**: Morningstar's public pages don't
> publish either, and the engine never fabricates a metric it can't source
> (see "Data dictionary" below) — the recommendations output instead carries a
> per-finalist **manual-verification checklist** (Sortino top-quartile, manager
> tenure ≥3y) so these unscrapeable checks are never silently dropped. Also
> new: because Morningstar's holdings table caps at ~100 rows, the ≤10% overlap
> gate can pass on incomplete data, so the report now includes each pick's
> holdings coverage, a rigorous worst-case overlap upper bound, and an
> `overlap_uncertain_pairs` flag for any pair it cannot certify within budget
> (the gate itself is unchanged — this is a reporting safeguard that tells you
> exactly when to verify externally). Rolling-return consistency (Step 4) is
> covered by **Stage 3 of the runbook**: `selection/nav_rolling_check.py`
> fetches each FINALIST's full AMFI NAV history and applies the rolling-window
> gates (≥90% of 3Y windows positive; worst 5Y window ≥ 0) plus a 5Y Sortino
> readout — importing the math and thresholds from the otherwise-dormant
> `selection/mf_select.py` so the two can never drift. The full-universe
> NAV-based framework remains dormant (see "Dormant alternate engine" below);
> only this shortlist-level second opinion is live.

## End-to-end runbook (plain Python — no Claude required)

The entire pipeline is standalone Python. Claude skills are OPTIONAL wrappers
that run these exact same commands — in production you can run everything
below directly and pay zero model tokens.

```bash
# STAGE 0 — one-time setup (idempotent)
./setup.sh && source .venv/bin/activate
python -m pytest tests/ -q                        # all suites must pass

# STAGE 1 — SCRAPE (one canonical script, three modes; list + enrichment in one run)
# (a) one fund house (full report: list + every fund enriched):
python scraper/morningstar_fund_details.py --out ms_data --headless --workers 4 \
    --house "HDFC Asset Management Company Limited"
# (b) individual fund(s) within a house:
python scraper/morningstar_fund_details.py --out ms_data --headless \
    --house "Axis Asset Management Company Limited" \
    --fund "Axis Bluechip Fund Direct Plan Growth"
# (c) the FULL UNIVERSE (~47 houses; --direct-growth-only enriches just the
#     ~3.5k plan variants the engine uses instead of all ~14k — ~75% faster;
#     the script prints an estimate before enriching):
python scraper/morningstar_fund_details.py --out ms_data --headless --workers 6 \
    --all --direct-growth-only
# Workers are clamped to a machine-safe ceiling TWICE: a CPU ceiling (each is
# a full Chrome; too many starve renderers -> "Timed out receiving message
# from renderer") and a STARTUP RAM sizing (~1.2 GB per worker + 1 GB OS
# reserve — on an 8 GB laptop with ~3 GB free, --workers 6 becomes 1-2; that
# is the honest sustainable fleet, not a limitation of the flag). Requesting
# more workers than RAM sustains does NOT go faster: the run boots N browsers,
# memory collapses, and the governor pins it to 1 slot while the others
# thrash and restart. --force-workers bypasses both clamps at your own risk.
# MEMORY-LEAN FLAGS for low-RAM machines (all data the engine needs is kept):
#   --equity-holdings-only  skip the Bond holdings pager walk per fund (the
#                           engine consumes Equity rows + summary only)
#   --refresh-days 30       skip re-enriching funds scraped within 30 days
#   --direct-growth-only    enrich only the plan variants the engine uses
#   --recommendation-universe-only  enrich only the ~14 categories the engine
#                           scores; skips ~80% of funds (debt/index/sector/
#                           long-short) it never selects. LIST data still kept.
# Images/ads are never decoded (blocked at the browser level — every scraped
# value is DOM text or a title attribute) and each worker's Chrome is
# recycled every 25 funds so leaked renderer memory returns to the OS.
# House/fund names must match ms_data/filters.json EXACTLY. --limit N caps
# enrichment per house (testing aid). Re-runs refresh list data but PRESERVE
# previously enriched funds.
# --refresh-days 30: skip re-enriching funds scraped within the last 30 days
# (Morningstar's risk/holdings tables update ~monthly; cuts routine refresh
# runs from hours to minutes). Add it once you have a full baseline snapshot.
# RECOMMENDED on an 8 GB machine (workers auto-sized to RAM; ask for 2):
python scraper/morningstar_fund_details.py --out ms_data --headless --workers 2 \
    --all --direct-growth-only --equity-holdings-only --refresh-days 30

# STAGE 2 — RECOMMENDATION ENGINE (no browser, no network; deterministic)
python selection/mf_recommend.py --selftest       # must print SELFTEST PASS
python selection/mf_recommend.py --data ms_data --out recommendation_run
# -> recommendations.json (scores, gates, reasons, run_hash, bench of
#    pre-validated substitutes per pick) + recommendations.md
# -> ms_data/metrics_history.jsonl (one row per fund per run, appended and
#    deduped by enriched_at — see "Longitudinal history" below; --no-history skips it)
# --exclude 'Fund Name' (repeatable) bars a fund from selection (e.g. after a
# Stage 3 FAIL) and rebuilds the portfolio under full constraints.

# STAGE 3 — NAV ROLLING-RETURN SECOND OPINION on the finalists (network:
# AMFI daily NAVs via api.mfapi.in; no browser). Closes the "lucky window"
# gap: Stage 2 sees three point-in-time horizons, this recomputes CAGR over
# EVERY rolling 3Y/5Y window in each finalist's full NAV history.
python selection/nav_rolling_check.py --report recommendation_run/recommendations.json
# Checks the PICKS and the report's BENCH substitutes in one pass (--no-bench
# to skip the bench) -> per-fund PASS / FAIL / SHORT_HISTORY (young fund) /
# INCOMPLETE_HISTORY (data issue) / UNRESOLVED verdicts;
# nav_rolling_check.json next to the report (thresholds,
# windows, Sortino 5Y, payload SHA-256 per fund). Exit code 0 only if all
# PICKS pass (bench verdicts are contingency info, never fail the run).
# A pick FAIL -> promote a PASSing substitute from that pick's bench (already
# verified in this same pass), or re-run Stage 2 with --exclude 'failed fund'
# for a full constraint-checked re-selection, then re-run this stage.
# If a fund can't be confidently matched to its AMFI scheme code, pin it:
#   --map 'HDFC Flexi Cap Fund -Direct Plan - Growth Option=118955'

# STAGE 4 — ALLOCATION PLANNER (offline; the one stage that asks the human).
# Inputs: total amount + risk appetite + duration (flags or interactive
# prompts). Output: exact % and amount per fund, from an explicit risk x
# horizon template table -> allocation_plan.json + allocation_plan.md.
python selection/mf_allocate.py --report recommendation_run/recommendations.json \
    --amount 1000000 --risk moderate --years 15                        # lumpsum
# --frequency sip: --amount becomes the MONTHLY installment; --sip-day and
# --start-date are recorded in the plan as the SIP contract Stage 5 replays:
python selection/mf_allocate.py --report recommendation_run/recommendations.json \
    --amount 25000 --risk moderate --years 15 \
    --frequency sip --sip-day 5 --start-date 2026-07-05                # SIP
# Honest guards: refuses durations under 5y (all-equity is wrong for short
# money); a pick that FAILED Stage 3 BLOCKS the plan (--allow-failed records
# an override warning instead); unfilled buckets redistribute with a warning;
# any single fund over 40% draws a concentration warning.

# STAGE 5 — REBALANCING AUDIT (periodic, e.g. annually or on a market move).
# Re-run Stages 1-2 first for a FRESH snapshot, then:
python selection/mf_rebalance.py --plan recommendation_run/allocation_plan.json \
    --report recommendation_run/recommendations.json --data ms_data
# Reads the plan's own frequency/schedule automatically — lumpsum replays a
# single buy date; SIP replays every monthly installment (sip_day +
# sip_start_date) through today, each priced at ITS OWN historical NAV.
# --transactions FILE covers an irregular SIP (missed/changed installments,
# a lumpsum top-up mixed in) with the ACTUAL dated purchases instead.
# Audits the live portfolio on TWO independent trigger families:
#   DRIFT   — the 5/25 rule vs the buy-time plan (current values derived
#             frequency-aware as above, or --current/--transactions overrides)
#   QUALITY — the SAME machinery the money was invested through: fresh Stage 2
#             gates, live Stage 3 rolling re-check, held-fund overlap
# Quality outranks arithmetic: a fund failing today's gates/rolling check
# -> REPLACEMENT_REQUIRED (no trades computed; fix composition via the
# --exclude loop first). Drift-only breach -> exact buy/sell trades (or a
# pure-buy plan with --new-money). All within thresholds -> HOLD.
# Output: rebalance_plan.json/.md with per-fund reasoning for every verdict.
```

Order matters: Stage 1 → Stage 2 → Stage 3 → Stage 4, then Stage 5
periodically. The engine only sees funds the scraper enriched (it needs the
risk tables); re-run Stage 2 alone any time to re-score the same snapshot —
identical run_hash proves nothing drifted. Stage 3 is the post-engine audit
on the 3–4 finalists only: deterministic per download (NAVs update daily),
with each payload hash recorded. Stage 4 is pure arithmetic over your own
inputs — deterministic (`plan_hash`), and the only stage that takes manual
input by design. Stage 5 closes the loop: the portfolio is re-audited with
the same rigor it was bought with, on a schedule you choose.
(These pipeline "Stages" are unrelated to the "Framework Steps" 1–4 above —
Framework Steps describe the *investment logic*; Stages describe *when to run
which command*.)

### Claude skills ↔ python scripts mapping

| Claude wrapper (optional) | Runs exactly | When to prefer which |
|---|---|---|
| `/morningstar-scrape` skill | Stage 1 above | skill: Claude supervises, validates output, retries failures; script: zero token cost, cron-able |
| `/mf-recommend` skill | Stage 2 above + writes `model_judgment.md` | skill adds the model-judgment layer (interpretation, conviction, flags); script alone gives you everything deterministic |
| `mf-portfolio-loop` agent | Stages 2→3→4 in a closed loop | agent: automates the exclude-rebuild iteration after Stage 3 FAILs, applies the structural selection heuristics, hands back a verified allocation with the full hash chain; scripts: run each stage by hand as in the quick guide |

**Production guidance:** once stable, schedule the python steps directly
(cron/CI) — no Claude in the loop, no token spend. The capabilities that
genuinely use a model are `model_judgment.md` (qualitative inference over
`recommendations.json`) and the closed-loop orchestration between Stage 3
verdicts and Stage 2 exclusions; both are optional and additive — every
underlying action remains a deterministic CLI.

## Data acquisition: the single canonical scraper

`scraper/morningstar_fund_details.py` is the ONE scraping script — fund list +
per-fund detail enrichment consolidated. It feeds Framework Step 2 (the
Quantitative Filter) with everything scraped from
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

**Worker ceiling.** Each worker is a full multi-process Chrome; too many on
one machine starve renderers (`Timed out receiving message from renderer`)
and total throughput *falls* while the site's request rate climbs — more
workers past that point makes the run slower and ruder, not faster.
`--workers` is therefore clamped to `max(2, min(8, cpu_count - 2))` unless
`--force-workers` overrides it. Workers also self-heal: a browser crash or
renderer stall triggers a session restart with backoff (`SESSION_RESTART_ATTEMPTS`);
anything still unrecoverable is recorded per house/fund in the manifest
(`failed_fund_houses`, `fund_failures`) rather than aborting the whole run —
re-running the same command fills the gaps (see the two re-run flags below).

**`--direct-growth-only`** enriches only Direct+Growth plan variants — the
only ones the recommendation engine ever consumes (~1/4 of all variants,
since Regular/IDCW/Payout duplicates exist per scheme). List-level data
(NAV, category) is still captured for every fund regardless; only the
expensive per-fund detail-page enrichment is skipped for non-Direct-Growth
variants. Cuts total enrichment work roughly 75%.

**`--recommendation-universe-only`** enriches only funds whose `Category` is
one the engine actually scores — the union of its four bucket lists in
`DEFAULT_CONFIG["universe"]["buckets"]` (Flexi/Large/Mid/Small-Cap, Value,
Contra, Dividend Yield, Multi-Cap, and the allocation categories; 14 in all).
A fund in any other category (debt, index, sector, arbitrage, fund-of-funds,
long-short, ELSS, …) is dropped by the engine at selection time
([mf_recommend.py](selection/mf_recommend.py) `bucket_for(...) is None →
continue`), so enriching it is pure waste — on the current snapshot that's 82%
of Direct+Growth funds. Their cheap LIST-level data (NAV, category) is still
captured, so nothing disappears from the store. The scraper's allow-list
(`RECOMMENDATION_UNIVERSE_CATEGORIES`) is kept identical to the engine's
buckets by the `test_recommendation_universe_matches_engine` drift-guard test —
if the engine gains a bucket category and the scraper isn't updated, that test
fails rather than silently under-enriching. Opt-in; default enriches every
category. Combine with `--direct-growth-only` for the smallest sufficient set.

**`--refresh-days N`** skips re-enriching a fund whose `enriched_at` is
younger than N days. Rationale: Morningstar's risk/holdings tables update on
roughly a monthly cadence, while NAV changes daily — and NAV is already
refreshed by the (cheap) list phase on every run regardless of this flag. Re-
scraping all detail pages on every run buys almost nothing beyond what the
list phase already provides, at the full multi-hour cost. Without this flag
(default), every run re-enriches everything — safe, but expensive. A fund
that has never been enriched (no `enriched_at`) is always scraped — and so is
a fund whose saved enrichment is *incomplete* (the freshness skip only trusts
entries that pass `enrichment_issues`, below).

### Resource governor & watchdog (laptop-safe scraping)

Sized for real hardware: on an 8 GB machine, 6 parallel Chromes plus macOS
leaves very little headroom — RAM exhaustion, not CPU, is what crashes runs.
Three layers keep that impossible:

0. **Startup RAM sizing** (`ram_capped_workers`, pure + unit-tested): before
   any Chrome starts, the requested fleet is shrunk to what free memory
   sustains (~1.2 GB per worker after a 1 GB OS reserve; unknown memory fails
   open). Post-mortem of a real `--workers 6` full-universe run on 8 GB: the
   governor spent **67% of all samples throttled to 1 worker of 6** (and
   another 20% at 1 of 3), available RAM pinned at 1–2.5 GB, load1 peaking at
   8.9, three full pauses below 1 GB. Six browsers never ran six-wide — they
   paid boot-collapse-restart tax to do one worker's throughput. Sizing the
   fleet up front gives the same real throughput without the thrash. Memory
   is also spent leaner per worker: images/ads are never decoded (blocked via
   Chrome prefs — every scraped value is DOM text or a title attribute), and
   each enrich worker proactively recycles its Chrome every `RECYCLE_EVERY`
   (25) funds because SAL-page renderer memory only returns to the OS on
   process exit.
1. **Internal governor** (in the scraper): a monitor thread samples available
   memory every 15s, writes a heartbeat to `<out>/_resource_monitor.log`, and
   adjusts how many worker slots may run concurrently
   (`allowed_workers`: ≥3 GB → full fleet, 2–3 GB → half, 1–2 GB → one
   sequential worker, <1 GB → full pause). Workers gate on a slot per task;
   during a full pause they QUIT their browsers so the RAM is actually
   returned to the OS, then restart sessions when memory recovers. Alerts go
   to stdout + a macOS notification on every throttle change.
2. **External watchdog** (`scraper/resource_watchdog.py`): **auto-started by
   the scraper with every run — no manual step.** An independent process (so
   it can act even if the scraper itself wedges): keeps the same monitoring
   log alive, alerts below 2 GB available, and in a true emergency (<1 GB)
   freezes the entire scrape tree with SIGSTOP until memory recovers
   (> 3 GB), then thaws it. A thawed run self-heals: timed-out attempts fail,
   sessions restart, and the completeness audit re-extracts whatever was
   lost. It deduplicates itself across overlapping runs, terminates when the
   run completes, and logs its lifecycle to `<out>/_watchdog.log`. (It can
   still be run standalone: `python scraper/resource_watchdog.py`.)

### Data-completeness model (validate → audit → repair)

Lesson learned live: a render race once saved a fund with an empty holdings
summary as "success", stamped it `enriched_at`, and the engine then
recommended it — its 18% overlap with another pick invisible to the overlap
rule. Three layers now prevent that class of failure:

1. **Validate before save** (`enrichment_issues`, pure + unit-tested): an
   enrichment result with an empty holdings summary, an error'd/empty-with-
   positive-count Equity list, or all-empty risk tables is a FAILED attempt —
   never stamped `enriched_at`, so `--refresh-days` can never trust bad data.
   Deliberately strict on Equity (the overlap rule depends on it) and lenient
   where Morningstar itself exposes nothing: an empty Bond list with a
   positive summary count is accepted (the site often renders no Bond tab for
   small bond sleeves); index/debt funds with zero equity are legitimate.
2. **Single-table pages handled**: index/debt funds render NO Equity/Bond/
   Others switcher (one holdings type only) — the visible table is read
   directly and attributed to the type with the largest positive summary
   count, instead of erroring on the missing switcher.
3. **Audit + repair (the deferred retry queue)**: the main ENRICH pass gives
   each fund exactly ONE attempt — a failure is recorded (console shows
   `FAILED — queued for repair pass`) and the worker moves straight on to the
   next fund; it never stalls retrying a fund in place. After the whole
   universe is done, the audit re-checks every expected (house, fund) against
   what's actually complete on disk and re-extracts anything missing or
   incomplete, up to `REPAIR_ROUNDS` (2) times (repair rounds retry twice in
   place — the last chance is worth it). Deferring retries this way keeps
   workers moving, and retries hours later often succeed where an immediate
   retry would hit the same transient throttle/render race. Whatever still
   fails is printed and recorded in the manifest under
   `incomplete_enrichments` — a bad or missing enrichment can never pass
   silently.

Outputs in `ms_data/`: `filters.json` (all dropdown values), one
`<Fund_House>.json` per house (fund-name keys, same structure as always — the
CANONICAL store all consumers read), and `morningstar_factsheet.json` — the
snapshot manifest **header only** (`scraped_at`, per-house counts, enriched
count, failures, `incomplete_enrichments`, payload sha256 computed over the
combined per-house content, so it still pins the snapshot). Fund data is
deliberately NOT duplicated inside the manifest file — that duplication used
to double total disk for zero informational gain.

Note: Morningstar's public holdings table can display fewer rows than the
holdings-summary counts (e.g. 74 of 93 equity positions) — the scraper captures
exactly what the page displays.

> **Canonical pipeline:** `morningstar_fund_details.py` is the final, validated
> scraping script (field-level validation vs live pages: 90/90). Extend it;
> don't fork new scrapers.

## Recommendation engine — knowledge base

`selection/mf_recommend.py` (v1.3.0) turns the enriched snapshot into ranked,
explained recommendations. This section is the engine's knowledge base: every
attribute the scraper captures, how the engine uses it, and WHY — so future
optimization work starts from the reasoning, not just the code.

```bash
python selection/mf_recommend.py --selftest                       # SELFTEST PASS
python selection/mf_recommend.py --data ms_data --out recommendation_run
```

### Data dictionary — every scraped attribute and its job

| Attribute (per fund JSON) | Used for | Why |
|---|---|---|
| `Category` | bucket mapping (core/growth/aggressive/diversifier) | Framework Step 1 asset allocation: structural diversification precedes any fund merit |
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
| `holdings.Equity[].% Portfolio Weight` | pairwise overlap between picks (≤ 10%) | Framework Step 3: paying two expense ratios for the same stocks is diversification theatre |
| `holdings.Equity[].Equity Star Rating` | weight-averaged portfolio quality score | rates what the manager *owns right now*, complementing the return-based metrics which only rate what they owned in the past |
| `holdings.Equity[].Sector` | effective sector count (inverse-HHI), reported | intra-fund concentration context; cross-fund diversification is already enforced by category quotas |
| `holdings.Equity[].Share Change %` | captured (direction-signed) | churn signal; summarised better by Reported Turnover %, so not double-counted in scoring |
| `holdings_summary.% Assets in Top 10` | concentration score (lower better) | top-heavy funds carry idiosyncratic blow-up risk that σ/β understate |
| `holdings_summary.Reported Turnover %` | churn score (lower better) | high churn = transaction drag + style instability; long horizons reward patient managers |
| `holdings_summary.Equity/Bond/Total Holdings` | breadth, reported in metrics | context for concentration numbers |
| `risk_ratings` horizon keys (3Y/5Y/10Y) | horizon selection + alpha stability | see below |

### Attribute audit — scraped vs consumed (verified against the 2,532-fund snapshot)

Everything the engine's gates/score/selection need **is** captured, and
nothing needed is missing. The excess in the other direction is small and
known:

| Scraped but unused by the engine | Size in snapshot | Verdict |
|---|---|---|
| Bond holdings rows | 28,806 rows ≈ 4.4 MB (~11%) | the ONLY material excess — costs a button click + pager walk per fund with bonds. Skippable via `--equity-holdings-only` (engine reads Equity rows + summary only; empty Bond list is already a valid store state) |
| `Share Change %` cells | ≈ 2 MB | free (same row read); deliberately unscored — churn is covered by turnover; kept as a future-version hook |
| risk-table `Index` columns | 89% em-dash | free (same table read); documented future hook |
| drawdown `Peak`/`Valley` dates | ≈ 0.3 MB | free (same read as Max Duration, which IS used) |
| `Action` list column | trivial | list-phase artifact |

So the scrape is not over-fetching in any way that costs meaningful memory or
runtime *except* Bond rows, and that is now a flag. Everything else unused
arrives in the same DOM reads as used values — dropping it would save nothing.

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

### Universe filter (which funds enter scoring at all)

Applied before any gate — config key `universe` in `DEFAULT_CONFIG`. A fund
that fails this step never reaches the gates below; it's simply not in the
`universe_size` count.

| Filter | Type | Value |
|---|---|---|
| Plan name must include (ALL) | fixed substrings | `Direct`, `Growth` |
| Plan name must exclude (ANY) | fixed substrings | `IDCW`, `Inc Dis`, `Payout`, `Reinvestment`, `Regular` |
| Must be enriched | fixed | fund JSON must contain a `risk_ratings` key (scraper successfully enriched it) |
| Category → bucket mapping | fixed list, per bucket | **core:** `Flexi Cap`, `Large-Cap`, `Large & Mid- Cap`, `Focused Fund` · **growth:** `Mid-Cap` · **aggressive:** `Small-Cap` · **diversifier:** `Value`, `Contra`, `Dividend Yield`, `Multi-Cap`, `Multi Asset Allocation`, `Aggressive Allocation`, `Dynamic Asset Allocation`, `Balanced Allocation` |

A `Category` value that doesn't appear in any bucket list (e.g. Debt, Gold/
Commodity, International, Sectoral/Thematic categories not listed above) is
dropped from the universe entirely — it is structurally out of scope for this
core/growth/aggressive/diversifier framework, not a merit judgment.

### Gates (capital protection first — nothing scores past a failed gate)

Applied to every universe fund at its `horizon_used` — config key `gates`.
**Fixed** = an absolute number compared directly to the fund's own metric.
**Relative** = a multiplier applied to the fund's *own category's* value at
the same horizon, so the effective threshold moves with the category.

| Gate | Type | Configured value | Condition to PASS | Rationale |
|---|---|---|---|---|
| `data_complete` | fixed | — (presence check) | all gate inputs present at some horizon (nearest-complete-horizon rule) | missing metrics are never guessed |
| `alpha_vs_category` | relative (delta vs category) | `alpha_vs_category_min_excess = 0.0` | `alpha_excess > 0.0` | skill vs peers is the entry ticket |
| `sharpe_vs_category` | relative (delta vs category) | `sharpe_vs_category_min_excess = 0.0` | `sharpe_excess ≥ 0.0` | risk-adjusted edge vs peers |
| `sharpe_beats_risk_free` | fixed | `sharpe_min_absolute = 0.0` | `Sharpe ≥ 0.0` | Morningstar Sharpe is already net of risk-free, so this IS the risk-free hurdle |
| `r_squared_reliability` | fixed | `r_squared_min = 70.0` | `R² ≥ 70.0` | below this, α/β are statistical noise |
| `beta_within_bucket_band` | fixed, per bucket | core `1.00` · growth `1.15` · aggressive `1.15` · diversifier `1.05` | `beta ≤ band[bucket]` | core money must be defensive; satellites may run hotter |
| `high_beta_alpha_compensation` | fixed, conditional | `high_beta_threshold = 1.00`; compensation `= 1.00` | if `beta > 1.00` then `alpha_excess ≥ 1.00` (else auto-pass) | extra market risk must be paid for by extra skill |
| `downside_capture_cap` | fixed | `downside_capture_absolute_max = 110.0` | `downside_capture ≤ 110.0` | worst-market hard ceiling, independent of category |
| `downside_capture_vs_category` | relative (× category) | `downside_capture_vs_category_tolerance = 1.05` | `downside_capture ≤ downside_capture_cat × 1.05` | may not be a worse-than-peers loser |
| `drawdown_vs_category` | relative (× category) | `drawdown_vs_category_tolerance = 1.10` | `max_drawdown ≥ max_drawdown_cat × 1.10` (both negative — i.e. fund's drawdown magnitude ≤ 1.10× category's) | realized worst case must stay within category norms |

All ten checks must pass simultaneously (`gates.passed = all(checks)`) —
there's no partial credit. Failing even one gate removes the fund from
scoring/selection; the exact failed check(s) per fund are recorded in the
report's `excluded_by_gates` list.

### Selection constraints (applied to gate survivors, after scoring)

| Constraint | Type | Configured value |
|---|---|---|
| Target portfolio size | fixed | `target_count = 4` |
| Bucket quotas | fixed | `1` core, `1` growth, `1` aggressive, `1` diversifier |
| Max funds per AMC | fixed | `max_funds_per_amc = 1` |
| Max funds per category | fixed | `max_funds_per_category = 1` |
| Max pairwise equity-holdings overlap | fixed | `max_pairwise_overlap_pct = 10.0%` |

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
`bucket_priority` order (core → growth → aggressive → diversifier — Framework
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

### Bench & closed-loop exclusions (v1.4)

The engine still recommends 3–4 funds — never more. Deliberately: the picks
are a **constraint-satisfying portfolio** (bucket quotas, one-per-AMC,
one-per-category, ≤10% pairwise overlap), not a leaderboard. Widening to 7–8
"so verification can trim later" was considered and rejected — it would (a)
force the overlap rule across 28 pairs instead of 6, reaching further down
the ranking for weaker picks, (b) allow the perverse case where a strong fund
is excluded for conflicting with a fund later trimmed anyway, and (c) leave
"which 4 of the 8 survive?" as an unsolved second selection problem. Instead,
two mechanisms cover the "what if a finalist fails external verification?"
case:

**Bench (`bench` in the report — reporting only, selection untouched).** For
each pick, up to `reporting.bench_alternates_per_pick` (default 2)
next-ranked gate survivors in the SAME bucket that clear every selection
constraint **against the other picks** — checked as a drop-in substitution
(the candidate may conflict with the pick it replaces, never with the picks
that stay; that's why the bench is not just "next by score"). Stage 3
verifies bench funds alongside the picks in the same pass, so a failed pick
usually has a **pre-verified substitute** waiting — zero extra iterations.

**Closed-loop exclusions (`--exclude 'Fund Name'`, repeatable; or
`selection.exclude_funds` in a `--config` file).** The rigorous alternative:
bar the failed fund from SELECTION (it stays ranked/scored for transparency,
skipped with an `excluded_by_config` decision in the log) and re-run — the
engine rebuilds the whole portfolio under full constraints. Use this instead
of the bench when the swap could cascade (a new core pick can change which
growth/aggressive funds are compatible — observed in practice: excluding one
core fund reshaped the entire selection). The exclusion list is part of
`config_hash`, so an excluded run is provably distinct and reproducible.

Rule of thumb: **bench for the surgical one-slot swap, `--exclude` for the
structural rebuild.** Both keep gates, scoring and the 3–4-fund contract
untouched.

### Near-miss watchlist (v1.3 — visibility, NOT relaxation)

Any bright-line gate produces funds that miss by a hair, and the report used
to show only *which* gate a fund failed, not *by how much* — so a fund 0.4
short on R² looked identical to one 8pp deeper than its category on drawdown.
The `near_misses` report section closes that gap **without ever moving a
threshold**: it lists every fund that failed at most `reporting.
near_miss_max_failed_gates` gate(s) (default **1** — the true near-misses),
each with the failed gate's `value`, `threshold` and signed `margin`, sorted
closest-miss-first. The engine also records a per-check `margin` on every fund
in `ranking[].gates` (≥ 0 on the passing side, < 0 on the failing side).

This is deliberately a **reporting layer, not a gate change**. A near-miss
fund still failed a capital-protection gate and is never scored or selected;
the tier exists so a human (or the model-judgment layer) can *see* how narrow
a rejection was and decide whether it warrants a deliberate, documented config
change — rather than the engine silently relaxing a line. Relaxing a threshold
doesn't remove near-misses, it relocates them (the next fund just below the
new line becomes the heartbreaker); margin visibility gives the judgment call
to a human while the pass/fail line stays bright and hash-provable. `margin`
is diagnostic only — the boolean gate checks in `apply_gates` remain the sole
source of pass/fail truth, and `gate_display()` mirrors the thresholds for
readability only.

### Bucket-role note: the diversifier beta band and hybrids

A frequent near-miss cluster is **Aggressive Allocation** (equity-heavy hybrid)
funds failing the diversifier beta band (1.05): ~83% of them sit just over it
(median β ≈ 1.11). This is the band working **as intended**, not a
miscalibration. The diversifier bucket exists to add low-correlation ballast
(Framework Step 1); an aggressive hybrid running β > 1.05 is equity beta in a
hybrid wrapper, not ballast. The band correctly *passes* genuinely defensive
Dynamic-Asset-Allocation / balanced-advantage funds (β ≤ 1.05) and low-beta
equity-style diversifiers (Value/Contra/Multi-Cap/Dividend Yield, median β
0.9–1.0), while *rejecting* hybrids that don't actually diversify. The only
principled refinement (deferred, not applied — it changes the universe) would
be to drop `Aggressive Allocation` from the diversifier bucket mapping so such
funds stop appearing as pseudo-candidates in `excluded_by_gates` at all; the
current picks do not change either way (the diversifier slot is filled by a
Multi-Cap fund, and no gate-passing hybrid was ever in contention).

### Determinism rules (apply to any future change)

No RNG; no wall-clock inside the hashed payload (`generated_at` recorded but
excluded from `run_hash`); floats through `r6()` before comparison; every
ordering ends in a fund-name tie-break; config and inputs hashed into the
manifest so identical `(snapshot, config)` provably yields identical output.

### Longitudinal history (`ms_data/metrics_history.jsonl`)

Every engine run appends one JSON line per **universe fund** (gate survivors
AND gate-excluded funds alike — "this fund used to pass, now it doesn't" is
often the more interesting long-term signal) to `metrics_history.jsonl`,
unless `--no-history` is passed. Each row carries the same decision-relevant
metrics used for gates/scoring (`HISTORY_METRIC_FIELDS`) plus `score` and
`gates_passed`/`failed_checks` — deliberately NOT the raw holdings rows or
risk-table blobs, which live only as *current state* in the per-house JSON,
so each row stays tiny (~300–500 bytes) no matter how long the file grows.

**Dedup key: the fund's `enriched_at` timestamp from the scrape — never
wall-clock.** Re-running the engine any number of times against an unchanged
`ms_data` snapshot appends nothing (each fund's `enriched_at` hasn't moved);
re-scraping one house next month appends exactly that house's funds on the
next engine run. This makes the file a genuine longitudinal record —
enabling future trend gates ("alpha excess declining N refreshes running")
and backtesting whether past recommendations aged well — without polluting
it with duplicate rows from repeated identical runs (verified live: a
background re-enrichment mid-session produced exactly one new row, for
exactly the one fund that had actually changed).

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
- **v1.3.0** — near-miss transparency (reporting only; gates/scoring/selection
  unchanged, so picks are byte-identical to v1.2 on the same snapshot — only
  `run_hash` moves, attributable to the version + new `near_misses` field). Adds
  a signed per-check `margin` to every `ranking[].gates` entry and a
  `near_misses` watchlist (funds failing ≤ `reporting.near_miss_max_failed_gates`
  gates, closest-miss-first, with value/threshold/margin). See "Near-miss
  watchlist" above. Also documents the bucket-role finding: the diversifier
  beta band is correctly rejecting equity-heavy hybrids (Aggressive Allocation
  ~83% over the 1.05 band), not miscalibrated — dropping that category from the
  diversifier mapping is a deferred option that would not change current picks.
- **v1.4.0** — bench + closed-loop exclusions (see "Bench & closed-loop
  exclusions" above). Reporting gains `bench` (pre-validated same-bucket
  substitutes per pick, drop-in-substitution semantics) verified by Stage 3
  alongside the picks; selection gains `selection.exclude_funds` /
  CLI `--exclude` (fund barred from selection only — still ranked, decision
  logged as `excluded_by_config`, hashed into `config_hash`). Gates, scoring
  and the 3–4-fund selection contract unchanged; on the same snapshot with no
  exclusions, picks are byte-identical to v1.3. Also from this era: overlap
  truncation safeguards (`holdings_coverage`, `overlap_upper_bound_pct`,
  `overlap_uncertain_pairs`), the per-pick `manual_verification` checklist,
  and the Stage 3 NAV rolling-return check (`nav_rolling_check.py`).
- **Post-v1.2.0 addendum** (no version bump — infrastructure, not a change to
  gates/scoring/selection, so `config_hash`/`run_hash` behavior is unaffected):
  added `metrics_history.jsonl` longitudinal tracking (see "Longitudinal
  history" above) and the scraper's `--refresh-days` / `--direct-growth-only`
  cost controls (see "Parallelism & safety model" above).
- Unused-but-captured (candidates for future versions): per-holding
  `Share Change %` trend, `First Bought` dates (not scraped), bond-holding
  credit quality (not published in the scraped table), Index columns of the
  risk tables (mostly em-dash on Morningstar India).

> **Disclaimer:** this repo produces data and arithmetic, not investment
> advice. Past performance does not predict future returns; mutual fund
> investments are subject to market risk.

## Post-engine verification: NAV rolling-return check (`nav_rolling_check.py`)

**The process to follow after every recommendation run (runbook Stage 3):**

```bash
python selection/nav_rolling_check.py --report recommendation_run/recommendations.json
```

**Why this stage exists.** The Morningstar engine judges each fund on three
point-in-time horizons (3Y/5Y/10Y *as of today*). Those are snapshots: a fund
can clear all three because today's window endpoints happen to be favourable,
while hiding an 18-month stretch underwater that a long-term holder would have
lived through. `nav_rolling_check.py` closes that gap for the finalists by
downloading each fund's **full daily NAV history** (AMFI data via the free
`api.mfapi.in` JSON mirror — no browser, no scraping) and recomputing CAGR
over *every* rolling window, stepping back 30 days at a time — typically
100+ overlapping windows per fund instead of 3 snapshots.

**Gates applied per finalist** (thresholds imported from `mf_select.py`'s
config — one source of truth, drift-guarded by
`test_evaluate_thresholds_come_from_dormant_framework`):

| Check | Pass condition | Catches |
|---|---|---|
| Rolling 3Y windows | ≥ 90% of windows CAGR > 0 | funds that spent long stretches losing money |
| Rolling 5Y worst window | worst window CAGR ≥ 0 | a 5-year holder who'd still have lost money |
| Sortino 5Y | reported, not gated | downside-adjusted return (top-quartile-vs-category stays a manual check — no category composite is fetched) |

**Verdicts and what to do with them** (the tool checks the picks AND the
report's `bench` of pre-validated substitutes in the same pass — see "Bench &
closed-loop exclusions" in the knowledge base; `--no-bench` restricts to
picks; the exit code gates on picks only):
- `PASS` — rolling history corroborates the engine's pick.
- `FAIL` on a pick — the fund has a path-dependence problem the snapshot
  engine could not see. Two remedies, in order of preference: (1) promote a
  `PASS`ing substitute from that pick's bench — it already clears every
  selection constraint against the other picks and was verified in this same
  run; (2) if the bench can't cover it, re-run the engine with
  `--exclude 'failed fund name'` for a full constraint-checked re-selection,
  then re-run this stage on the new report.
- `SHORT_HISTORY` — a **genuinely young fund**: complete daily NAVs and a fresh
  latest NAV, but fewer than 7 years of life (so no rolling 5Y windows yet).
  This is a fund *characteristic*, not a data gap — increasingly common for
  post-2020 launches (WhiteOak, Jio, Bandhan, NJ, several new AMCs). For a
  >10-year horizon it is a soft caution (unproven over a full market cycle):
  the reported age lets you judge whether ~2 years or ~6.5 years, and you
  decide manually whether to accept an unproven fund or prefer a seasoned peer.
- `INCOMPLETE_HISTORY` — the **data itself looks wrong**, not the fund's age:
  either *sparse* (far below the ~250/yr daily NAV cadence → a truncated
  download) or *stale* (latest NAV is over a month old → a merged/closed
  scheme, or a wrong scheme-code match). This is a tooling/mapping problem to
  fix — re-download, or pin the correct scheme with `--map` — before trusting
  any verdict on the fund. Distinguishing this from `SHORT_HISTORY` is exactly
  the "genuinely new fund vs missing history" question: a young fund is a real
  finding to weigh; incomplete data is a bug to fix.
- `UNRESOLVED` — the fund name couldn't be confidently matched to exactly one
  AMFI Direct-Growth scheme. The tool **never guesses**: it prints the
  candidates; pin the right one with `--map 'Fund Name=schemeCode'`.

**Honesty properties** (same contract as the engines): scheme matching
requires every identity token plus Direct+Growth and rejects
Regular/IDCW/bonus variants; ambiguity is surfaced, never resolved by
guessing; a too-young fund is reported, never passed; the output
(`nav_rolling_check.json`) records the thresholds, per-fund window counts,
date ranges and a SHA-256 of each downloaded payload, so a verdict is
reproducible for a given download (NAVs update daily, so cross-day runs
legitimately differ — that's the data, not the logic). Exit code is `0` only
when **all** finalists PASS, so the step can gate an automated pipeline.

**What this stage still does NOT cover** (remains on the manual checklist in
`recommendations.md`): Sortino *vs category* (top-quartile), manager tenure,
and full-holdings overlap for truncated tables.

## Allocation planner: amount / risk / duration → exact breakdown (`mf_allocate.py`)

**The final stage (runbook Stage 4) — and the only one designed to take
manual input.** Three inputs (total amount in rupees, risk appetite,
intended holding duration — CLI flags, or interactive prompts when omitted)
become a PRACTICAL per-fund breakdown: **whole-number percentages** (always
summing to exactly 100 — 44%, never 43.75%) and **round amounts** in
`--step` multiples (default ₹1,000 — figures you can actually type into an
order), while the whole amount is still invested: when the total itself
isn't a multiple of the step, the sub-step residue is added to the largest
allocation and noted in the plan. `--step 1` restores rupee-exact splits.

```bash
# lumpsum (default)
python selection/mf_allocate.py --report recommendation_run/recommendations.json \
    --amount 1000000 --risk moderate --years 15

# SIP: --amount is the MONTHLY installment; --sip-day/--start-date become
# part of the plan's contract so Stage 5 can reconstruct every installment
python selection/mf_allocate.py --report recommendation_run/recommendations.json \
    --amount 25000 --risk moderate --years 15 \
    --frequency sip --sip-day 5 --start-date 2026-07-05
```

**Lumpsum and SIP share the identical bucket-weighting math** — a SIP is
just the same split applied to a recurring monthly amount instead of a
one-time total. The only difference is what the plan records: a SIP plan's
`inputs` carries `frequency: "sip"`, `sip_day`, and `sip_start_date`, which
[Stage 5](#rebalancing-audit-drift--full-quality-re-check-mf_rebalancepy)
reads back to value a SIP-funded portfolio correctly (pricing each
installment at *its own* historical NAV, not one NAV for the whole
position). `--frequency` defaults to `lumpsum`, so nothing changes for
existing commands.

**How the split is decided — an explicit lookup table, not a formula.**
Bucket weights come from a 3×3 template table (`ALLOCATION_TEMPLATES`:
risk profile × horizon band, every row unit-tested to sum to 100), e.g.:

| Risk / band | core | growth | aggressive | diversifier |
|---|---|---|---|---|
| conservative, 5-10y | 50 | 10 | 0 | 40 |
| moderate, 10-15y | 40 | 25 | 15 | 20 |
| aggressive, 15y+ | 25 | 30 | 30 | 15 |

Longer runway earns more growth/aggressive; conservative profiles anchor on
core + diversifier (the engine's calmest equity bucket). Within a bucket,
funds split equally. If a bucket has no pick (a valid engine outcome), its
weight redistributes proportionally across the filled buckets — warned in
the plan, never silent.

**Honest guards (warnings report, gates block — nothing is silently "fixed"):**
- **Duration < 5 years → refused.** An all-equity portfolio is the wrong
  instrument for short money; that's a suitability statement, not a config
  default. Park short money in debt/liquid instruments (outside this repo).
- **A pick that FAILED Stage 3 → plan BLOCKED** until you swap in a bench
  substitute or `--exclude`-re-run the engine. `--allow-failed` downgrades
  the block to a recorded warning — the override itself is in the plan.
- **Stage 3 never run / verdict missing → warned** (allocation proceeds, the
  gap is on record). `SHORT_HISTORY` picks carry their young-fund note.
- **Concentration** — any single fund above 40% (usually a consequence of
  unfilled buckets) draws a warning to complete the portfolio first.
- **Zero-weight bucket** — e.g. an aggressive pick under (conservative,
  5-10y) gets 0%: shown at 0% with a warning, never silently dropped.

**Outputs**: `allocation_plan.json` (inputs, band, template used, per-fund
rows, warnings, source report's `run_hash`, and a `plan_hash` over
everything but the timestamp) plus a human-readable `allocation_plan.md`
table. Deterministic: same report + same three inputs ⇒ same plan.

## Rebalancing audit: drift + full quality re-check (`mf_rebalance.py`)

**Stage 5 — the periodic loop-closer.** The allocation plan is the buy-time
contract; this stage audits the live portfolio against it AND re-runs the
same quality machinery the money was originally invested through, so a
rebalance can never quietly keep funding a fund that no longer deserves it.

```bash
# re-run Stages 1-2 first (fresh snapshot), then:
python selection/mf_rebalance.py --plan recommendation_run/allocation_plan.json \
    --report recommendation_run/recommendations.json --data ms_data
```

**Trigger family 1 — DRIFT (arithmetic vs the plan).** The classic 5/25
rule: a fund breaches when its current weight is off target by more than
5 percentage points absolute (`--drift-pp`) **or** 25% of its target weight
(`--drift-rel` — this is what catches a small 5%-target sleeve quietly
growing to 8%).

Current values are derived **frequency-aware**, replaying the plan's own
contract (this is the fix for the earlier lumpsum-only limitation — a SIP
has many purchase dates at many NAVs, and a single-date formula silently
misvalues it):
- **lumpsum**: units = invested amount ÷ NAV on the buy date, value = units
  × latest NAV;
- **sip**: the plan's `sip_day` + `sip_start_date` are replayed into every
  monthly installment date through today (or `--as-of`), each priced at
  *that installment's own* historical NAV, and the units summed;

every number — per-installment NAV, date, units — is recorded in
`value_derivations`. Two escape hatches, in priority order: `--current
'Fund=value'` when you know the exact value better than any derivation
(highest priority), or `--transactions FILE` (a JSON map of each fund's
actual dated purchases) for an irregular SIP — a missed month, a changed
installment amount, or a lumpsum top-up mixed into an otherwise-regular SIP.

**Trigger family 2 — QUALITY (the same process as the initial investment):**
- **fresh Stage 2 gates** — a held fund now in the new report's
  `excluded_by_gates` FAILS (the exact failed checks are quoted); a fund
  absent from the fresh snapshot is `INCONCLUSIVE` (rescrape — the tool
  never guesses either way);
- **live Stage 3 rolling re-check** on every held fund (same gates, same
  math; `--skip-rolling` skips it and says so);
- **held-fund overlap** recomputed from the fresh scrape — funds whose
  portfolios have converged past 10% since purchase draw a warning.

**Decision precedence — quality outranks arithmetic:**

| Verdict | Meaning | What you get |
|---|---|---|
| `HOLD` (exit 0) | within thresholds, quality intact | the reasons why nothing needs doing |
| `REBALANCE_REQUIRED` | quality intact, drift breached | exact whole-rupee BUY/SELL per fund restoring plan weights; with `--new-money N` targets include the fresh cash — large enough N gives a pure-buy plan (no sells → no capital gains realised; the sells case carries an explicit STCG/LTCG warning). For a SIP-funded portfolio the output also suggests adjusting the *next installment's* fund-wise split toward the target weights as a no-transaction alternative to the trades below |
| `REPLACEMENT_REQUIRED` | a held fund fails today's gates or rolling check | **no trades computed** — composition first: the output lists the exact engine loop (Stage 2 `--exclude 'failed fund'` → 3 → 4 on the proceeds → re-run Stage 5) |
| `INCONCLUSIVE` | a held fund is missing from the fresh snapshot | blocked until rescraped — missing data is never papered over |

Every per-fund action (`HOLD` / `TRIM` / `ADD` / `REPLACE` / `INCONCLUSIVE`)
ships with its full reasoning — weight vs target with drift in pp and %,
fresh-gate status with rank/score or failed-check names, rolling verdict —
in both `rebalance_plan.json` and the human-readable `rebalance_plan.md`.
The output records the source plan's `plan_hash`, the fresh report's
`run_hash`, NAV payload hashes, and its own `rebalance_hash` — the same
reproducibility contract as every other stage.

## Dormant alternate engine: NAV-based deterministic selection (`mf_select.py`)

> **Status: DORMANT PIPELINE, LIVE LIBRARY.** `mf_select.py` is **not part of
> the live workflow** — the only pipeline that runs is
> `morningstar_fund_details.py` (scrape) → `ms_data/` → `mf_recommend.py`
> (recommend). Its own end-to-end pipeline has never been fed: `mf_dataset/`
> does not exist and `benchmarks/`, `holdings/` hold only `.gitkeep`. **It is
> retained for two reasons only:** (1) it is the live math library — 
> `mf_recommend.py` imports `pairwise_overlap`, `percentile_ranks`, `r6` and
> the SHA-256 hashers, and `nav_rolling_check.py` (runbook Stage 3) imports
> `rolling_cagrs`, `sortino` and the gate thresholds — so the file cannot be
> deleted; (2) it is the reference implementation for a possible future
> full-universe NAV-based revival. Its own end-to-end pipeline produces no
> output in the current process — the only live use of its rolling/Sortino
> math is the shortlist-level Stage 3 check.

`selection/mf_select.py` is a second, independent selection framework — rules
-based, reproducible selection of 3–4 Direct-Growth funds for a >10-year
horizon, using raw NAV history (via the `indian-mf-data` dataset tooling)
instead of Morningstar's scraped risk tables. It predates the Morningstar
pipeline above and would again become useful if you ever build a raw
NAV/benchmark-TRI dataset (it computes Sortino and rolling-return consistency,
which the Morningstar engine cannot). It ships its own gates, scoring,
selection and determinism guarantees — see `docs/FRAMEWORK.md` for the full
specification and honesty notes (survivorship bias in category composites,
realistic overlap thresholds, Direct-plan history starting 2013).

```
selection/
├── mf_select.py            # this framework (stdlib only, no dependencies)
└── framework_config.json   # all rules: universe, gates, weights, selection
docs/FRAMEWORK.md           # full specification and honesty notes
tests/test_mf_select.py     # pytest wrapper over the deterministic selftest
benchmarks/                 # you supply: <KEY>.csv with date,value (TRI!)
holdings/                   # you supply: <scheme_code>.csv with isin,weight_pct
```

```bash
# 1. verify the framework's own guarantees (no data needed)
python selection/mf_select.py --selftest

# 2. build the NAV dataset (indian-mf-data tooling, separate from this repo)
python scripts/build_mf_dataset.py --out mf_dataset

# 3. run a selection
python selection/mf_select.py \
    --dataset mf_dataset \
    --config selection/framework_config.json \
    --benchmarks benchmarks/ \
    --holdings holdings/ \
    --out runs/2026-07
```

The report (`runs/2026-07/report.json`) contains the full ranking, per-gate
results, the selection with a decision log for every skip, the overlap
matrix, and a `run_hash` + `config_hash` + per-file `input_hashes` manifest —
same reproducibility contract as the Morningstar engine: identical config
hash + input hashes ⇒ identical `run_hash`, always; if two runs differ, the
hashes tell you it's the data, never the logic. Pin `"as_of"` in the config to
freeze a snapshot for repeatable audits.

**What you must supply, and why the framework won't guess:**
- **Benchmark TRI CSVs** (`benchmarks/<KEY>.csv`, columns `date,value`) from
  niftyindices.com — NAV data contains no index, and using a price index
  instead of TRI fabricates ~1.2–1.5%/yr of fake alpha. Without these, the
  alpha-vs-index and capture gates are marked *not evaluated*.
- **Holdings CSVs** (`holdings/<scheme_code>.csv`, columns `isin,weight_pct`)
  from AMC monthly portfolio disclosures. Without these, pairwise overlap
  cannot be computed and the framework falls back to one-fund-per-category /
  one-per-AMC and says so — it never silently claims "<10% overlap".

## Claude skills & agents

Two project skills and one agent automate the workflow end-to-end (see
[.claude/skills/README.md](.claude/skills/README.md)): `/morningstar-scrape`
refreshes the data snapshot; `/mf-recommend` re-runs the engine on it; and
the **`mf-portfolio-loop` agent** (`.claude/agents/mf-portfolio-loop.md`)
drives the closed loop Stage 2 → Stage 3 → Stage 4 — excluding any pick that
fails NAV verification and rebuilding until the portfolio passes, then
allocating — with the settled structural heuristics from the 2026-07 build
encoded so future runs start from the known route. All are
designed so a future-dated re-run is reproducible and any change in output is
attributable to data (`input_hashes`) or rules (`config_hash`), never chance. A live site is a changing input, so run-to-run identity is not
promised — but the manifest hash lets any downstream framework run pin exactly which
snapshot it consumed. **Be polite**: keep the conservative defaults (single session,
sequential requests, `ACTION_DELAY` between interactions) and check Morningstar's
Terms of Use and robots.txt before running.