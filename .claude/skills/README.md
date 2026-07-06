# Claude Skills & Agents — MFRecommendationEngine

Project-local skills and agents for Claude Code. Skills live in
`.claude/skills/<skill-name>/SKILL.md` (invoke with `/<skill-name>` or plain
natural language); agents live in `.claude/agents/<name>.md` (spawned as
subagents for multi-stage jobs).

**Skills are thin wrappers over standalone python scripts** — they run the
exact commands in README.md's "Quick guide: end-to-end in four commands
(+ periodic rebalance)". The pipeline is FIVE stages:

| Stage | Script | Coverage |
|---|---|---|
| 1. Scrape | `scraper/morningstar_fund_details.py` | `/morningstar-scrape` skill |
| 2. Recommend | `selection/mf_recommend.py` | `/mf-recommend` skill |
| 3. Verify (NAV rolling) | `selection/nav_rolling_check.py` | **mf-portfolio-loop agent** |
| 4. Allocate (lumpsum/SIP) | `selection/mf_allocate.py` | **mf-portfolio-loop agent** |
| 5. Rebalance (periodic) | `selection/mf_rebalance.py` | run directly (README quick guide §5) |

In production, run the python scripts directly (cron/CI) with zero token
cost; only the optional `model_judgment.md` interpretation layer and the
closed-loop orchestration need Claude.

## Available skills

### 1. `/morningstar-scrape` — refresh the data snapshot

**What it does:** runs the single canonical scraper
(`scraper/morningstar_fund_details.py`) against morningstar.in — factsheet
list scrape AND per-fund Detailed Portfolio + Risk & Rating enrichment in one
run, with `--workers N` parallel browsers.

**How to run:** invoke the skill and state the scope, e.g.
- "scrape HDFC fund house" (`--house`, full report for that house)
- "scrape just this fund of Axis" (`--house ... --fund ...`)
- "refresh the full universe" (`--all` — warn: hours even parallelised)

**Expected output:**
- `ms_data/filters.json` — all filter dropdown values (4 groups)
- `ms_data/<Fund_House>.json` — one JSON per house; keys are fund names;
  enriched funds additionally carry `detailed_portfolio` + `risk_ratings`
- `ms_data/morningstar_factsheet.json` — combined list snapshot whose manifest
  must show `failed_fund_houses: {}` and internally consistent totals
  (~14,000 schemes for the full universe)

**Runtime:** list scrape ~15–30 min (6 workers); enrichment ~1 min per fund.

### 2. `/mf-recommend` — run the recommendation engine

**What it does:** runs `selection/mf_recommend.py` over the enriched snapshot:
capital-protection gates → weighted percentile scoring → bucket-diversified
selection (core/growth/aggressive/diversifier) with AMC, category and
holdings-overlap constraints — each pick explained by a
`recommendation_reason` built from the same numbers that drove its score.

**How to run:** invoke the skill; it always runs `--selftest` first (must
print `SELFTEST PASS`), then the real run. To refresh recommendations at a
future date, run `/morningstar-scrape` first, then this skill.

**Expected output:**
- `recommendation_run/recommendations.json` — ranking with per-gate
  checks, gate exclusions with named failed checks, recommendations with
  reasons, per-pick bench of pre-validated substitutes, overlap matrix (+
  worst-case upper bounds), and a manifest (`config_hash`, `input_hashes`,
  `run_hash`)
- `recommendation_run/recommendations.md` — human-readable summary
- An **empty recommendation list is a valid, honest outcome** (nothing passed
  the gates, or too few houses are enriched yet) — it is never padded.
- Picks are NOT final until Stage 3 verifies them — see the agent below.

### 3. `mf-portfolio-loop` agent — verified portfolio + allocation (closed loop)

**What it does** (`.claude/agents/mf-portfolio-loop.md`): drives Stage 2 →
Stage 3 → Stage 4 iteratively. When a pick FAILS the Stage 3 NAV
rolling-return check, it feeds the fund back through the engine with
`--exclude` and rebuilds under full constraints — repeating until every pick
PASSES — then produces the allocation plan (lumpsum or SIP) with the hash
chain (`run_hash` → nav verdicts → `plan_hash`) cited in its conclusion.

**Why an agent and not a skill:** the loop is judgment-bearing between
deterministic steps — retry-vs-map on UNRESOLVED, surfacing SHORT_HISTORY to
the human, recognising structural collapse (a blended Large&Mid core
overlap-blocking every Mid-Cap; the diversifier bucket being unfillable next
to a large-cap core). Those heuristics — learned from the 2026-07 build —
are encoded in the agent so future runs start from the settled route instead
of rediscovering it. Every ACTION it takes is still one of the deterministic
CLIs; the agent never hand-patches picks or plans.

## Determinism, in one paragraph

The scraping *process* is deterministic (fixed steps, fixed locators, sorted
keys, atomic writes) but its *data* is a live-site snapshot pinned by manifest
hashes (`payload_sha256`, `scraped_at`). Stages 2, 4 and 5 are fully
deterministic end-to-end: identical snapshot + config/inputs ⇒ identical
`run_hash` / `plan_hash` / `rebalance_hash` (exclusions are hashed into
`config_hash`, so a closed-loop rebuild is provably a different run). Stage 3
is deterministic *per NAV download* (NAVs update daily; each payload's sha256
+ date range is recorded). So any change between two runs is provably
attributable — diff `input_hashes` (data moved) or `config_hash` (rules
moved); chance is never an explanation. The agent inherits this: it only
ever acts through these CLIs, and its conclusions must cite the hash chain.

## Prerequisites (skills and agent)

```bash
./setup.sh                    # creates .venv, installs selenium/webdriver-manager/pytest
source .venv/bin/activate
python -m pytest tests/ -q    # 113 tests, all browserless, must pass
```

Chrome must be installed for scraping (headless mode is used by default).
The engine itself needs no browser and no network.
