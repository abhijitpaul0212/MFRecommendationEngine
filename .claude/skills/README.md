# Claude Skills — MFRecommendationEngine

Project-local skills for Claude Code. Each skill lives in
`.claude/skills/<skill-name>/SKILL.md` and is auto-discovered; invoke one by
typing `/<skill-name>` in Claude Code (or just ask in natural language — the
skill descriptions route matching requests automatically).

**Skills are thin wrappers over standalone python scripts** — they run the
exact commands in README.md's "End-to-end runbook". Run sequence:
`/morningstar-scrape` (step 1: the single canonical scraper —
`scraper/morningstar_fund_details.py`, modes: one house / individual fund /
full universe) → `/mf-recommend` (step 2: `selection/mf_recommend.py` +
model judgment). In production, run the python scripts directly (cron/CI)
with zero token cost; only the optional `model_judgment.md` interpretation
layer needs Claude.

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
- `ms_data/recommendation_run/recommendations.json` — ranking with per-gate
  checks, gate exclusions with named failed checks, recommendations with
  reasons, overlap matrix, and a manifest (`config_hash`, `input_hashes`,
  `run_hash`)
- `ms_data/recommendation_run/recommendations.md` — human-readable summary
- An **empty recommendation list is a valid, honest outcome** (nothing passed
  the gates, or too few houses are enriched yet) — it is never padded.

## Determinism, in one paragraph

The scraping *process* is deterministic (fixed steps, fixed locators, sorted
keys, atomic writes) but its *data* is a live-site snapshot pinned by manifest
hashes (`payload_sha256`, `scraped_at`). The recommendation engine is fully
deterministic end-to-end: identical snapshot + config ⇒ identical `run_hash`.
So any change between two runs is provably attributable — diff `input_hashes`
(data moved) or `config_hash` (rules moved); chance is never an explanation.

## Prerequisites (both skills)

```bash
./setup.sh                    # creates .venv, installs selenium/webdriver-manager/pytest
source .venv/bin/activate
python -m pytest tests/ -q    # 36 tests, all browserless, must pass
```

Chrome must be installed for scraping (headless mode is used by default).
The engine itself needs no browser and no network.
