---
name: morningstar-scrape
description: Run the canonical Morningstar India scraping pipeline — factsheet list scrape (all 47 fund houses, parallel browsers) plus per-fund Detailed Portfolio & Risk/Rating enrichment — producing the enriched ms_data/ JSON snapshot. Use when the user asks to scrape/refresh Morningstar mutual fund data, fetch latest NAV/holdings/risk metrics, or rebuild ms_data.
---

# Morningstar India MF scraping pipeline

## Background (read before running)

This repo's **canonical, validated scraper is exactly ONE script** —
`scraper/morningstar_fund_details.py` (list scrape + per-fund enrichment
consolidated). Do not write new scraping code or improvise selectors; the
locator strategies were validated against the live site (90/90 field-level
checks vs real page data). Per fund house it runs two phases in one browser:

1. **LIST**: navigates morningstar.in → Funds → Factsheet (ASP.NET WebForms +
   UpdatePanel partial postbacks + select2 filters), selects the Fund House
   with Category/Distribution/Structure at their All-defaults, 100 rows/page,
   paginates until `<a disabled="disabled">Next ></a>` — collecting the row
   data `{Fund Name: {Action, Category, Latest NAV, NAV Date}}` AND each
   fund's detail URL in a single pass.
2. **ENRICH**: per fund, opens `detailed-portfolio.aspx` and
   `risk-ratings.aspx` (derived from the fund anchor — equivalent to clicking
   the tabs), extracts the holdings summary, Equity+Bond holdings rows
   ('Other' excluded; attributes: Holdings, % Portfolio Weight, Share Change %,
   Equity Star Rating, Sector) across all pager pages, and the 3-Yr/5-Yr/10-Yr
   Risk & Volatility + Market Volatility tables. Nests results under each fund
   without touching list-level attributes. Re-runs refresh list attributes but
   PRESERVE previously enriched funds.

### Site quirks the scripts already handle (do NOT re-solve)
- Popups (subscription modal, cookie bar, webpush) — layered best-effort
  dismissal; never an error.
- The holdings type switch is a **button group** (Equity/Bond/Others) at
  desktop width; the popup dropdown only appears on small viewports. All type
  tables are pre-rendered and visibility-toggled; the scripts read only the
  visible table whose first header is "Holdings".
- "No records found…" empty-state rows are filtered (`EMPTY_ROW_MARKERS`).
- Morningstar's public holdings table can display fewer rows than the
  holdings-summary counts (e.g. 74 of 93) — capturing what's displayed is
  correct; do not chase the difference.
- Star ratings are read from the `Star rating : N` title attribute; visible
  cells are SVG-only. Share Change % is stored signed (down-arrow → negative).
- Funds with zero holdings of a type (e.g. Bond for an index fund) yield `[]`.

## How to run

```bash
# one-time setup (idempotent)
./setup.sh && source .venv/bin/activate

# MODE 1 — one fund house (full report: list + every fund enriched)
python scraper/morningstar_fund_details.py --out ms_data --headless --workers 4 \
    --house "<exact house name from ms_data/filters.json>"

# MODE 2 — individual fund(s) within a house (--fund repeatable)
python scraper/morningstar_fund_details.py --out ms_data --headless \
    --house "<exact house name>" --fund "<exact fund name>"

# MODE 3 — full universe (~47 houses; prefer --direct-growth-only, which
#          enriches only the ~3.5k plan variants the recommendation engine
#          uses instead of all ~14k — ~75% less work, still HOURS on a
#          first-ever run). Add --refresh-days N on ROUTINE re-runs once a
#          baseline snapshot exists — skips funds enriched within N days
#          (Morningstar's risk/holdings tables update ~monthly), turning
#          repeat runs into a minutes-not-hours refresh:
python scraper/morningstar_fund_details.py --out ms_data --headless --workers 6 \
    --all --direct-growth-only --refresh-days 30

# unit tests (no browser needed)
python -m pytest tests/test_morningstar_parse.py tests/test_fund_details_parse.py -v
```

House and fund names must match the scraped data exactly (e.g. "Axis Asset
Management Company Limited", NOT "Axis Mutual Fund" — an unknown house name
errors out; an unknown fund name is reported as SKIPPED). `--limit N` caps
enrichment per house for testing. Keep delays at defaults (be polite to the
site); prefer background execution for long runs. Re-runs are safe: list-level
attributes refresh, previously enriched funds are preserved.

Resilience notes: `--workers` is clamped to a machine-safe ceiling (each
worker is a full Chrome; too many starve renderers and cause "Timed out
receiving message from renderer" — `--force-workers` overrides at your own
risk, do not suggest it by default). Workers self-heal: a browser crash or
renderer stall restarts the session with backoff, and anything that still
fails is recorded per house/fund in the manifest (`failed_fund_houses`,
`fund_failures`) instead of aborting the run — check those after long runs
and re-run the same command to fill gaps.

Resource safety is FULLY AUTOMATIC (README "Resource governor & watchdog" —
no manual step, do not tell the user to start anything): every run gets
(1) the internal governor — samples free RAM every 15s, heartbeats to
`<out>/_resource_monitor.log`, throttles worker slots as memory tightens and
fully pauses (browsers released) below 1 GB, resuming automatically, with
console + macOS-notification alerts on every change; and (2) the external
watchdog, auto-spawned as a separate process (`<out>/_watchdog.log`) — it
alerts the user below 2 GB, freezes/thaws the whole scrape tree in a true
emergency, deduplicates itself, and exits when the run completes.

Completeness guarantees (README "Data-completeness model" has the full
rationale): every enrichment result is VALIDATED before saving (empty
holdings summary / error'd Equity / empty risk tables = failed attempt,
retried, never stamped `enriched_at`); index/debt funds with no
Equity/Bond/Others switcher are read as single-table pages; after the enrich
phase an AUDIT double-checks every expected fund against what's complete on
disk and re-extracts anything missing (2 repair rounds); leftovers land in
the manifest's `incomplete_enrichments`. After any long run, check that field
— if non-empty, re-running the same command repairs it (`--refresh-days`
never trusts an incomplete entry, regardless of its timestamp).

## Expected output (validate before declaring success)

- `ms_data/filters.json` — 4 dropdown groups; ~47 fund houses.
- `ms_data/<Fund_House>.json` — one per house; keys are fund names.
- `ms_data/morningstar_factsheet.json` — manifest HEADER only (fund data
  lives solely in the per-house files; the payload sha256 pins the combined
  snapshot). Check it after every run: `failed_fund_houses` must be `{}`,
  `incomplete_enrichments` should be empty, and `total_schemes` must equal
  the sum of per-house counts (~14,000 for the full universe).
- After enrichment, each processed fund gains `detail_url`,
  `detailed_portfolio` (holdings_summary + holdings.Equity/Bond lists),
  `risk_ratings` (3Y/5Y/10Y, each with 5 risk metrics × Investment/Category/
  Index plus capture ratios, max drawdown, drawdown dates) and `enriched_at`.
- Spot-validate: equity row count for an index fund should equal its equity
  summary count; risk tables should contain Alpha/Beta/R-Squared/Sharpe
  Ratio/Standard Deviation.

## Determinism contract

The PROCESS is deterministic — fixed navigation steps, fixed locators, sorted
JSON keys, atomic writes, collision-safe merges (nothing is ever lost or
overwritten; a crash mid-run keeps all completed work). The DATA is a live-site
snapshot and changes as Morningstar updates: run-to-run identity of values is
not promised — instead every combined output carries a manifest
(`payload_sha256`, `scraped_at`, per-house counts) that pins exactly which
snapshot downstream consumers used. Never fabricate or infer values the page
did not display; missing cells are em-dashes and must stay that way.
