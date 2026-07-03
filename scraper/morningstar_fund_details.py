#!/usr/bin/env python3
"""
morningstar_fund_details.py — THE single canonical Morningstar India scraper
for MFRecommendationEngine (list scrape + per-fund enrichment, consolidated).

Three generation modes (CLI):
  --house "<name>"                 full JSON report for one fund house
  --house "<name>" --fund "<name>" one individual fund (repeatable --fund)
  --all                            the full universe (~47 houses), parallelised

Whatever the mode, output is the SAME per-house JSON store as always:
  ms_data/<Fund_House>.json  with fund-name keys, e.g.
  "Axis Aggresive Hybrid Fund Direct Growth": {
      "Action", "Category", "Latest NAV", "NAV Date",          <- list level
      "detail_url", "detailed_portfolio", "risk_ratings",      <- enrichment
      "enriched_at"
  }
plus ms_data/filters.json (all dropdown values) and
ms_data/morningstar_factsheet.json (combined manifest pinning the snapshot).

Pipeline per house (one browser does both phases for its houses):
  A. LIST: navigate morningstar.in -> Funds -> Factsheet, select the house
     with Category/Distribution/Structure at their All-defaults, 100 rows per
     page, paginate until <a disabled="disabled">Next ></a>. Rows and fund
     detail URLs are collected in a SINGLE pass over the table.
  B. ENRICH: for each fund, open its detailed-portfolio.aspx and
     risk-ratings.aspx pages (derived from the fund anchor — identical to
     clicking the tabs) and extract the holdings summary, Equity+Bond holdings
     across all pager pages ('Other' excluded), and the 3Y/5Y/10Y risk tables.

Parallelism (--workers N): houses are dealt round-robin across N independent
Chrome instances for the list phase; enrichment tasks (house, fund, url) are
then dealt round-robin across N browsers. All merges into shared state happen
under one lock with atomic temp-file writes, so per-house JSONs are always
valid and a crash keeps every completed fund. Re-runs REFRESH list-level
attributes but PRESERVE existing enrichment for funds not re-enriched.

Architecture layers (unit-tested without a browser — tests/test_morningstar_parse.py
and tests/test_fund_details_parse.py):
  1. PURE CORE: rows_to_json, is_next_disabled, normalize_scheme_name,
     attach_to_catalog, merge_house_into, atomic_write_json, derive_tab_urls,
     parse_star_rating, signed_share_change, nest_fund_details, ...
  2. PAGE OBJECTS: FactsheetPage (WebForms UpdatePanel list) and
     FundDetailPage (JS-rendered SAL detail pages).
  3. ORCHESTRATOR: MorningstarScraper (modes, workers, merging, manifest).

Site quirks handled (validated 90/90 field-level vs live pages — do not re-solve):
  * ASP.NET partial postbacks: never reuse a WebElement across a postback;
    wait on PageRequestManager and re-find fresh.
  * select2 filter widgets: native <select> + change event, select2 UI fallback.
  * Popups (subscription modal / cookie bar / webpush): layered best-effort
    dismissal, never an error.
  * "No records found..." empty-state rows filtered (EMPTY_ROW_MARKERS).
  * Holdings type switch is a BUTTON GROUP (Equity/Bond/Others) at desktop
    width (popup dropdown only on small viewports); all type tables are
    pre-rendered and visibility-toggled — read only the visible one whose
    first header is "Holdings".
  * Star ratings come from the "Star rating : N" title attr (cells are SVG);
    Share Change % is stored signed (down-arrow -> negative).
  * Morningstar's public holdings table can display fewer rows than the
    summary counts — capturing what is displayed is correct.

Responsible use: single polite session per worker, ACTION_DELAY between
interactions. Check Morningstar's Terms of Use / robots.txt before running.
Determinism: the PROCESS is deterministic; the DATA is a live snapshot pinned
by the combined manifest's payload sha256 + scraped_at.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import urljoin

BASE_URL = "https://www.morningstar.in/default.aspx"
ACTION_DELAY = 2.0          # polite pause between interactions (seconds)
POSTBACK_TIMEOUT = 30
PAGE_LOAD_TIMEOUT = 60
MAX_PAGES_GUARD = 500       # hard stop against infinite pagination loops
SAL_WAIT_TIMEOUT = 45       # SAL widgets fetch data after page load
SECONDS_PER_FUND_EST = 50   # for the runtime estimate printed before enrichment

HOLDING_TYPES = ("Equity", "Bond")        # 'Other' intentionally excluded
YEAR_TABS = (("for3Year", "3Y"), ("for5Year", "5Y"), ("for10Year", "10Y"))

HOLDINGS_TABLE_CSS = "table[summary*='Holdings component']"
RISK_TABLE_CSS = ".sal-risk-volatility-measures__dataTable table"
MARKET_TABLE_CSS = "table[summary*='Upside Capture Ratio']"

# ---------------------------------------------------------------------------
# Locator map — user-supplied XPaths first, semantic fallbacks second.
# ---------------------------------------------------------------------------
LOCATORS = {
    "menu_funds":        ['//*[@id="mnuFunds"]',
                          '//a[contains(@href,"funds") and contains(.,"Funds")]'],
    "menu_factsheet":    ['//*[@id="factsheet"]',
                          '//a[contains(@href,"factsheet")]'],
    "filter_panel":      ['//*[@id="ctl00_ContentPlaceHolder1_upnlFunds"]/div/div/div[1]/div'],
    "btn_go":            ['//*[@id="ctl00_ContentPlaceHolder1_btnGo"]'],
    "rowcount_select2":  ['//*[@id="select2-ctl00_ContentPlaceHolder1_ddlShowRowCount-container"]'],
    "rowcount_native":   ['//*[@id="ctl00_ContentPlaceHolder1_ddlShowRowCount"]'],
    "header_row":        ['//*[@id="ctl00_ContentPlaceHolder1_lstvFunds_trHeaders"]'],
    "fundname_header":   ['//*[@id="ctl00_ContentPlaceHolder1_lstvFunds_lnkFundName"]/div'],
    "table_body":        ['//*[@id="ctl00_ContentPlaceHolder1_upnlFunds"]/div/div/div[2]/div/table/tbody'],
    "pager":             ['//*[@id="ctl00_ContentPlaceHolder1_pagerFunds"]'],
    "pager_next_indexed":['//*[@id="ctl00_ContentPlaceHolder1_pagerFunds"]/a[5]'],
    "pager_next_by_text":['//*[@id="ctl00_ContentPlaceHolder1_pagerFunds"]'
                          '//a[normalize-space(.)="Next >" or contains(normalize-space(.),"Next")]'],
    # Known popup close controls, tried in order; all optional.
    "popup_closers":     ['//button[contains(@class,"close")]',
                          '//a[contains(@class,"close")]',
                          '//*[@id="closeBtn" or @id="btnClose" or @class="popup-close"]',
                          '//div[contains(@class,"modal") and contains(@style,"display: block")]'
                          '//button[contains(.,"×") or contains(.,"Close") or contains(.,"No thanks")]',
                          '//button[contains(.,"Accept") or contains(.,"I Agree") or contains(.,"Got it")]'],
}

# The four filter dropdowns, their default value, and label hints (order on
# page: Fund House, Category, Distribution, Structure).
FILTERS = [
    {"key": "fund_house",   "default": None,                 "label_hints": ["fund house", "amc"]},
    {"key": "category",     "default": "All Categories",     "label_hints": ["category"]},
    {"key": "distribution", "default": "All Distributions",  "label_hints": ["distribution"]},
    {"key": "structure",    "default": "All Structures",     "label_hints": ["structure"]},
]

# Enrichment keys preserved across list re-scrapes (see merge in list phase).
ENRICHMENT_KEYS = ("detail_url", "detailed_portfolio", "risk_ratings", "enriched_at")


# ===========================================================================
# 1) PURE CORE — testable without selenium
# ===========================================================================

# Empty-state template rows that ASP.NET ListView renders inside the results
# table when a filter combination has no funds.
EMPTY_ROW_MARKERS = ("no records found",)


def rows_to_json(headers, rows, fund_name_col=0):
    """[[cell,...],...] -> {fund_name: {header: value}}. Deterministic.

    * Header list defines the schema; short rows are padded with "".
    * Rows whose fund-name cell is empty are dropped (spacer rows).
    * Rows whose fund-name cell is an empty-state placeholder (e.g. "No records
      found...") are dropped — they are not funds.
    * Duplicate fund names get a numeric suffix so nothing is silently lost.
    """
    out = {}
    for row in rows:
        cells = [c.strip() for c in row]
        if len(cells) < len(headers):
            cells += [""] * (len(headers) - len(cells))
        name = cells[fund_name_col]
        if not name:
            continue
        low = name.lower()
        if any(marker in low for marker in EMPTY_ROW_MARKERS):
            continue
        key, n = name, 2
        while key in out:
            key, n = f"{name} ({n})", n + 1
        out[key] = {h: cells[i] for i, h in enumerate(headers) if i != fund_name_col}
    return out


def is_next_disabled(next_link_attrs, next_link_present=True):
    """Pagination stop condition. Morningstar disables Next as
    <a disabled="disabled">Next &gt;</a>; absence of the link also stops."""
    if not next_link_present:
        return True
    if next_link_attrs is None:
        return True
    disabled = next_link_attrs.get("disabled")
    if disabled is not None and str(disabled).lower() in ("", "true", "disabled"):
        return True
    cls = next_link_attrs.get("class", "") or ""
    return "disabled" in cls.lower().split()


def normalize_scheme_name(name):
    """Normalise fund names for cross-source matching (AMFI vs Morningstar).
    Lowercase, strip plan/option suffixes and punctuation, collapse spaces."""
    s = name.lower()
    s = re.sub(r"\[.*?\]", " ", s)
    s = re.sub(r"\b(direct|regular)\b", " ", s)
    s = re.sub(r"\b(plan|option|growth|idcw|dividend|payout|reinvestment)\b", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def attach_to_catalog(catalog, ms_data):
    """Merge Morningstar attributes into an mf_dataset catalog by normalised
    name. Returns (enriched_catalog, match_report). Pure, deterministic.
    Matching is exact-on-normalised-name only — no fuzzy guessing."""
    ms_norm = {normalize_scheme_name(k): (k, v) for k, v in sorted(ms_data.items())}
    enriched, matched, unmatched_catalog = [], [], []
    for s in catalog:
        key = normalize_scheme_name(s.get("scheme_name", ""))
        s2 = dict(s)
        if key in ms_norm:
            src_name, attrs = ms_norm[key]
            s2["morningstar"] = {"source_name": src_name, **attrs}
            matched.append(s.get("scheme_code"))
        else:
            unmatched_catalog.append(s.get("scheme_name"))
        enriched.append(s2)
    used = {normalize_scheme_name(s.get("scheme_name", "")) for s in catalog}
    unmatched_ms = sorted(k for nk, (k, _) in ms_norm.items() if nk not in used)
    report = {
        "catalog_total": len(catalog),
        "matched": len(matched),
        "unmatched_catalog_names": sorted(n for n in unmatched_catalog if n),
        "unmatched_morningstar_names": unmatched_ms,
    }
    return enriched, report


def payload_hash(obj):
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def merge_house_into(combined, house_data):
    """Merge one house's {name: attrs} into `combined` IN PLACE, losing
    nothing (identical -> no-op; conflicting -> numeric-suffixed key).
    Caller holds the lock across threads. Returns count of new keys."""
    added = 0
    for name, attrs in house_data.items():
        if combined.get(name) == attrs:
            continue
        key, n = name, 2
        while key in combined and combined[key] != attrs:
            key, n = f"{name} ({n})", n + 1
        combined[key] = attrs
        added += 1
    return added


def atomic_write_json(path, obj):
    """Temp file + os.replace(): a reader (or crash) never sees a half-written
    file — always the old valid snapshot or the new valid snapshot."""
    tmp = f"{path}.tmp.{os.getpid()}.{threading.get_ident()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, sort_keys=True, ensure_ascii=False)
    os.replace(tmp, path)


def clean(s):
    return re.sub(r"\s+", " ", s or "").strip()


def derive_tab_urls(fund_href, base=BASE_URL):
    """Fund-list anchor href -> the two detail-tab URLs (sibling .aspx pages)."""
    absolute = urljoin(base, (fund_href or "").split("#")[0])
    stem = absolute.rsplit("/", 1)[0]
    return {
        "detailed_portfolio": f"{stem}/detailed-portfolio.aspx",
        "risk_ratings": f"{stem}/risk-ratings.aspx",
    }


def parse_star_rating(title_text):
    """'Star rating : 3' -> '3'; anything without a digit -> None."""
    m = re.search(r"(\d+)", title_text or "")
    return m.group(1) if m else None


def signed_share_change(value, direction):
    """('2.00','decrease') -> '-2.00' — the on-screen arrow is never lost."""
    v = clean(value)
    if not v or v in ("—", "–", "-"):
        return v or None
    if direction == "decrease" and not v.startswith("-"):
        return f"-{v}"
    return v


def normalize_metric(name):
    """Risk-table row header cleanup; the R² row renders as 'R'+sup'2'."""
    n = clean(name)
    if re.sub(r"\s+", "", n).lower() == "r2":
        return "R-Squared"
    return n


def nest_fund_details(attrs, detail_url=None, detailed_portfolio=None,
                      risk_ratings=None, enriched_at=None):
    """Non-destructive: copy of existing attrs + enrichment keys added."""
    out = dict(attrs)
    if detail_url:
        out["detail_url"] = detail_url
    if detailed_portfolio is not None:
        out["detailed_portfolio"] = detailed_portfolio
    if risk_ratings is not None:
        out["risk_ratings"] = risk_ratings
    if enriched_at:
        out["enriched_at"] = enriched_at
    return out


def safe_house_name(house):
    return re.sub(r"[^A-Za-z0-9]+", "_", house).strip("_")


def merge_list_preserving_enrichment(existing, fresh):
    """New snapshot of the list REFRESHES list-level attrs but PRESERVES the
    enrichment already on disk for funds still present. Funds that left the
    site's list are dropped (the file mirrors the current snapshot)."""
    merged = {}
    for name, attrs in fresh.items():
        old = existing.get(name) or {}
        keep = {k: old[k] for k in ENRICHMENT_KEYS if k in old}
        merged[name] = {**attrs, **keep}
    return merged


# ===========================================================================
# 2) PAGE OBJECTS — selenium imported lazily so the pure core stays
#    importable in browserless CI.
# ===========================================================================

def _selenium():
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys
    from selenium.webdriver.common.action_chains import ActionChains
    from selenium.webdriver.support.ui import WebDriverWait, Select
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.common.exceptions import (
        TimeoutException, NoSuchElementException, StaleElementReferenceException,
        ElementClickInterceptedException,
    )
    return locals()


def make_driver(headless=True):
    S = _selenium()
    opts = S["webdriver"].ChromeOptions()
    if headless:
        opts.add_argument("--headless=new")
    opts.add_argument("--window-size=1600,1000")
    opts.add_argument("--disable-notifications")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option(
        "prefs", {"profile.default_content_setting_values.notifications": 2})
    d = S["webdriver"].Chrome(options=opts)
    d.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
    return d


class FactsheetPage:
    """Page object for the Morningstar India fund factsheet LIST screen
    (ASP.NET WebForms + UpdatePanel partial postbacks + select2 widgets)."""

    def __init__(self, driver, delay=ACTION_DELAY):
        self.d = driver
        self.delay = delay
        self.S = _selenium()

    # ---------- primitives ----------
    def find(self, key, timeout=15, required=True):
        """Try each locator variant for `key`; first present match wins."""
        By, WDW, EC = self.S["By"], self.S["WebDriverWait"], self.S["EC"]
        last_err = None
        for xp in LOCATORS[key]:
            try:
                return WDW(self.d, timeout).until(
                    EC.presence_of_element_located((By.XPATH, xp)))
            except self.S["TimeoutException"] as e:
                last_err = e
        if required:
            raise self.S["TimeoutException"](f"locator '{key}' not found via any variant") from last_err
        return None

    def click(self, key_or_el, timeout=15):
        el = self.find(key_or_el, timeout) if isinstance(key_or_el, str) else key_or_el
        try:
            el.click()
        except (self.S["ElementClickInterceptedException"], Exception):
            self.d.execute_script("arguments[0].scrollIntoView({block:'center'});"
                                  "arguments[0].click();", el)
        time.sleep(self.delay)

    def _wait_postback_complete(self, timeout=POSTBACK_TIMEOUT):
        """Block until the ASP.NET partial postback finishes."""
        js = ("try { return Sys.WebForms.PageRequestManager.getInstance()"
              ".get_isInAsyncPostBack() === false; } catch(e) { return true; }")
        self.S["WebDriverWait"](self.d, timeout).until(
            lambda d: d.execute_script(js))
        time.sleep(self.delay)

    # ---------- popups ----------
    def dismiss_popups(self):
        """Best-effort, layered, never raises. Also scans iframes once."""
        By, Keys = self.S["By"], self.S["Keys"]
        def try_close_in_current_context():
            closed = False
            for xp in LOCATORS["popup_closers"]:
                try:
                    for el in self.d.find_elements(By.XPATH, xp):
                        if el.is_displayed():
                            el.click()
                            closed = True
                            time.sleep(0.5)
                except Exception:
                    continue
            return closed
        try:
            try_close_in_current_context()
            for frame in self.d.find_elements(By.TAG_NAME, "iframe")[:5]:
                try:
                    self.d.switch_to.frame(frame)
                    try_close_in_current_context()
                finally:
                    self.d.switch_to.default_content()
            self.d.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
        except Exception:
            pass

    # ---------- navigation ----------
    def open_factsheet(self):
        self.d.get(BASE_URL)
        time.sleep(self.delay)
        self.dismiss_popups()
        menu = self.find("menu_funds")
        self.S["ActionChains"](self.d).move_to_element(menu).perform()
        time.sleep(0.8)                     # hover menu animation
        try:
            self.click("menu_factsheet")
        except Exception:
            self.click(menu)                # click-to-open menus
            self.click("menu_factsheet")
        self.dismiss_popups()
        self.find("filter_panel", timeout=30)

    # ---------- filters ----------
    def _panel_selects(self):
        By = self.S["By"]
        panel = self.find("filter_panel")
        return panel.find_elements(By.TAG_NAME, "select")

    def read_filter_options(self):
        """{filter_key: [option texts]} for all four dropdowns."""
        selects = self._panel_selects()
        out = {}
        for spec, sel in zip(FILTERS, selects):
            opts = [o.text.strip() for o in
                    self.S["Select"](sel).options if o.text.strip()]
            out[spec["key"]] = opts
        return out

    def _select2_pick(self, native_select_el, text):
        By = self.S["By"]
        try:
            self.S["Select"](native_select_el).select_by_visible_text(text)
            self.d.execute_script(
                "arguments[0].dispatchEvent(new Event('change', {bubbles:true}));",
                native_select_el)
            return
        except Exception:
            pass
        sel_id = native_select_el.get_attribute("id")
        container = self.d.find_element(By.ID, f"select2-{sel_id}-container")
        container.click()
        time.sleep(0.5)
        for li in self.d.find_elements(By.XPATH, "//li[contains(@class,'select2-results__option')]"):
            if li.text.strip() == text:
                li.click()
                return
        raise RuntimeError(f"option '{text}' not found in select2 {sel_id}")

    def set_filters(self, fund_house):
        """Chosen fund house + defaults everywhere else, re-finding selects
        after each change because postbacks stale them."""
        for i, spec in enumerate(FILTERS):
            target = fund_house if spec["key"] == "fund_house" else spec["default"]
            selects = self._panel_selects()
            self._select2_pick(selects[i], target)
            self._wait_postback_complete()

    def click_go(self):
        self.click("btn_go")
        self._wait_postback_complete()

    def set_rowcount_100(self):
        native = self.find("rowcount_native", required=False)
        if native is not None:
            self._select2_pick(native, "100")
        else:
            self.click("rowcount_select2")
            time.sleep(0.5)
            By = self.S["By"]
            for li in self.d.find_elements(By.XPATH,
                    "//li[contains(@class,'select2-results__option')]"):
                if li.text.strip() == "100":
                    li.click()
                    break
        self._wait_postback_complete()

    # ---------- table ----------
    def read_headers(self):
        By = self.S["By"]
        row = self.find("header_row")
        headers = []
        for cell in row.find_elements(By.XPATH, "./th|./td"):
            t = cell.text.strip().replace("\n", " ")
            if t:
                headers.append(t)
        return headers

    def read_rows_and_urls(self):
        """Single pass over the results table: cell texts AND the fund detail
        URL from each row's anchor. Returns (rows, {fund_name: href})."""
        By = self.S["By"]
        body = self.find("table_body")
        rows, urls = [], {}
        for tr in body.find_elements(By.TAG_NAME, "tr"):
            cells = [td.text.strip() for td in tr.find_elements(By.XPATH, "./td|./th")]
            if any(cells):
                rows.append(cells)
            for a in tr.find_elements(By.XPATH, ".//a[contains(@href,'/mutualfunds/')]"):
                name, href = a.text.strip(), a.get_attribute("href")
                if name and href and name not in urls:
                    urls[name] = href
        return rows, urls

    def next_page(self):
        """Click Next if enabled. Returns False when pagination is done."""
        By = self.S["By"]
        link = None
        for key in ("pager_next_by_text", "pager_next_indexed"):
            try:
                for xp in LOCATORS[key]:
                    els = self.d.find_elements(By.XPATH, xp)
                    for el in els:
                        if "next" in el.text.lower():
                            link = el
                            break
                    if link is not None:
                        break
            except Exception:
                continue
            if link is not None:
                break
        if link is None:
            return False
        attrs = {"disabled": link.get_attribute("disabled"),
                 "class": link.get_attribute("class")}
        if is_next_disabled(attrs):
            return False
        self.click(link)
        self._wait_postback_complete()
        return True


class FundDetailPage:
    """Page object for /mutualfunds/<id>/<slug>/detailed-portfolio.aspx and
    risk-ratings.aspx — JS-rendered SAL components (possibly inside an
    iframe); every read waits for content in whichever frame it renders."""

    def __init__(self, driver, delay=ACTION_DELAY):
        self.d = driver
        self.delay = delay
        self.S = _selenium()
        self._popups = FactsheetPage(driver, delay)   # reuse popup dismissal

    # ---------- navigation / frame handling ----------
    def open(self, url, wait_css, timeout=SAL_WAIT_TIMEOUT):
        self.d.get(url)
        time.sleep(self.delay)
        self._popups.dismiss_popups()
        self._wait_sal(wait_css, timeout)

    def _wait_sal(self, css, timeout=SAL_WAIT_TIMEOUT):
        By = self.S["By"]
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.d.switch_to.default_content()
            if self.d.find_elements(By.CSS_SELECTOR, css):
                return
            for fr in self.d.find_elements(By.TAG_NAME, "iframe"):
                try:
                    self.d.switch_to.frame(fr)
                    if self.d.find_elements(By.CSS_SELECTOR, css):
                        return
                except Exception:
                    pass
                self.d.switch_to.default_content()
            time.sleep(1.0)
        raise TimeoutError(f"SAL content '{css}' not found within {timeout}s")

    def _js_click(self, el):
        self.d.execute_script(
            "arguments[0].scrollIntoView({block:'center'}); arguments[0].click();", el)
        time.sleep(self.delay)

    # ---------- holdings summary ----------
    def read_holdings_summary(self):
        By = self.S["By"]
        out = {}
        for pair in self.d.find_elements(By.CSS_SELECTOR,
                                         ".holdings-summary .sal-dp-pair"):
            try:
                try:  # names with sr-only duplicates keep only the visible div
                    name = pair.find_element(
                        By.CSS_SELECTOR, ".sal-dp-name div[aria-hidden='true']"
                    ).get_attribute("textContent")
                except Exception:
                    name = pair.find_element(
                        By.CSS_SELECTOR, ".sal-dp-name").get_attribute("textContent")
                value = pair.find_element(
                    By.CSS_SELECTOR, ".sal-dp-value").get_attribute("textContent")
            except Exception:
                continue
            name, value = clean(name), clean(value)
            if name and name not in out:   # page renders small+large screen copies
                out[name] = value
        return out

    # ---------- holdings table ----------
    def _active_holdings_table(self):
        """One table per holding type is pre-rendered (plus a recap table);
        the Equity/Bond/Others button group toggles visibility — 'the' table
        is the DISPLAYED one whose first column header is 'Holdings'."""
        By = self.S["By"]
        for tbl in self.d.find_elements(By.CSS_SELECTOR, HOLDINGS_TABLE_CSS):
            try:
                if not tbl.is_displayed():
                    continue
                first = tbl.find_elements(By.CSS_SELECTOR, "thead th")
                if first and clean(first[0].get_attribute("textContent")) == "Holdings":
                    return tbl
            except Exception:
                continue
        raise RuntimeError("no visible holdings table found")

    def _holdings_headers(self, tbl=None):
        By = self.S["By"]
        tbl = tbl if tbl is not None else self._active_holdings_table()
        return [clean(th.get_attribute("textContent"))
                for th in tbl.find_elements(By.CSS_SELECTOR, "thead th")]

    def _first_holding_name(self):
        By = self.S["By"]
        try:
            tbl = self._active_holdings_table()
        except Exception:
            return None
        for sel in ("tbody th a", "tbody th"):
            els = tbl.find_elements(By.CSS_SELECTOR, sel)
            if els:
                return clean(els[0].get_attribute("textContent"))
        return None

    def _row_record(self, tr, headers):
        By = self.S["By"]
        ths = tr.find_elements(By.CSS_SELECTOR, "th")
        if not ths:
            return None
        name = clean(ths[0].get_attribute("textContent"))
        if not name:
            return None
        tds = tr.find_elements(By.CSS_SELECTOR, "td")

        def td_for(pred):     # headers[0] is the Holdings th column
            for i, h in enumerate(headers[1:]):
                if pred(h) and i < len(tds):
                    return tds[i]
            return None

        rec = {"Holdings": name}
        td = td_for(lambda h: "Portfolio Weight" in h)
        rec["% Portfolio Weight"] = clean(td.get_attribute("textContent")) if td is not None else None

        td = td_for(lambda h: "Share Change" in h)
        if td is not None:
            cls = td.get_attribute("class") or ""
            direction = ("decrease" if "changeNegative" in cls
                         else "increase" if "changePositive" in cls else "none")
            m = re.search(r"-?\d+(?:\.\d+)?", clean(td.get_attribute("textContent")))
            rec["Share Change %"] = signed_share_change(m.group(0) if m else None, direction)
        else:
            rec["Share Change %"] = None

        td = td_for(lambda h: "Star Rating" in h)
        rating = None
        if td is not None:
            try:
                rating = parse_star_rating(td.find_element(
                    By.CSS_SELECTOR, "span[title*='Star rating']").get_attribute("title"))
            except Exception:
                rating = None
        rec["Equity Star Rating"] = rating

        td = td_for(lambda h: h == "Sector")
        rec["Sector"] = clean(td.get_attribute("textContent")) if td is not None else None
        return rec

    def read_holdings_rows(self):
        By = self.S["By"]
        tbl = self._active_holdings_table()
        headers = self._holdings_headers(tbl)
        rows = []
        for tr in tbl.find_elements(By.CSS_SELECTOR, "tbody tr"):
            rec = self._row_record(tr, headers)
            if rec:
                rows.append(rec)
        return rows

    # ---------- holdings pagination ----------
    def _page_select(self):
        """The visible mds page <select> with all-numeric options — one per
        holding type is rendered but only the active type's is displayed."""
        By = self.S["By"]
        for sel in self.d.find_elements(By.CSS_SELECTOR, "select.mds-select__input__sal"):
            try:
                if not sel.is_displayed():
                    continue
                opts = [o.get_attribute("value")
                        for o in sel.find_elements(By.TAG_NAME, "option")]
                if opts and all(v.isdigit() for v in opts):
                    return sel
            except Exception:
                continue
        return None

    def _page_values(self):
        sel = self._page_select()
        if sel is None:
            return ["1"]
        By = self.S["By"]
        return [o.get_attribute("value")
                for o in sel.find_elements(By.TAG_NAME, "option")] or ["1"]

    def _goto_page(self, value):
        sel = self._page_select()
        if sel is None:
            return
        before = self._first_holding_name()
        self.S["Select"](sel).select_by_value(value)
        self.d.execute_script(
            "arguments[0].dispatchEvent(new Event('change', {bubbles:true}));", sel)
        self._wait_change(self._first_holding_name, before)

    def _wait_change(self, probe, before, timeout=12):
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(1.0)
            try:
                if probe() != before:
                    return True
            except Exception:
                pass
        return False   # content may legitimately be unchanged; caller proceeds

    def read_all_holdings_pages(self):
        rows, seen = [], set()
        values = self._page_values()
        for i, v in enumerate(values):
            if i > 0:
                self._goto_page(v)
            for rec in self.read_holdings_rows():
                key = (rec.get("Holdings"), rec.get("% Portfolio Weight"))
                if key in seen:
                    continue
                seen.add(key)
                rows.append(rec)
        return rows

    # ---------- holding-type switch (Equity / Bond / Others) ----------
    def _type_buttons(self):
        """Desktop: mds button GROUP (active item carries ...__item-active__sal).
        Small viewports render a popup dropdown instead — see fallback."""
        By = self.S["By"]
        out = {}
        for b in self.d.find_elements(By.CSS_SELECTOR, "button[class*='mds-button-group__item']"):
            try:
                label = clean(b.get_attribute("textContent"))
                if label in ("Equity", "Bond", "Others", "Other") and b.is_displayed():
                    out[label.rstrip("s") if label == "Others" else label] = b
            except Exception:
                continue
        return out

    def select_holding_type(self, label):
        want = "Other" if label == "Others" else label
        group = self._type_buttons()
        if group:
            btn = group.get(want) or group.get(f"{want}s")
            if btn is None:
                raise RuntimeError(f"holding-type button '{label}' not found")
            if "item-active" in (btn.get_attribute("class") or ""):
                return                               # already selected
            before = self._first_holding_name()
            self._js_click(btn)
            self._wait_change(self._first_holding_name, before)
            return
        self._select_holding_type_via_dropdown(label)

    def _select_holding_type_via_dropdown(self, label):
        """Small-viewport fallback: popup button (aria-haspopup) + menu."""
        By = self.S["By"]
        btn = None
        for b in self.d.find_elements(By.CSS_SELECTOR, "button[aria-haspopup='true']"):
            if clean(b.get_attribute("textContent")) in ("Equity", "Bond", "Other", "Others"):
                btn = b
                break
        if btn is None:
            raise RuntimeError("holding-type control not found (group or dropdown)")
        if clean(btn.get_attribute("textContent")).rstrip("s") == label.rstrip("s"):
            return
        before = self._first_holding_name()
        self._js_click(btn)
        option = None
        for el in self.d.find_elements(
                By.XPATH,
                f"//button[normalize-space(.)='{label}'] | "
                f"//li[normalize-space(.)='{label}'] | "
                f"//span[normalize-space(.)='{label}']"):
            try:
                if el.is_displayed() and el != btn:
                    option = el
                    break
            except Exception:
                continue
        if option is None:
            raise RuntimeError(f"holding-type option '{label}' not found in menu")
        self._js_click(option)
        self._wait_change(self._first_holding_name, before)

    def read_detailed_portfolio(self):
        summary = self.read_holdings_summary()
        holdings = {}
        for htype in HOLDING_TYPES:
            try:
                self.select_holding_type(htype)
                holdings[htype] = self.read_all_holdings_pages()
            except Exception as e:
                # Zero holdings of a type -> no switch button at all: empty
                # list, not an error (e.g. Bond for an equity index fund).
                count = summary.get(f"{htype} Holdings", "").strip()
                if count in ("0", "—", "–", ""):
                    holdings[htype] = []
                else:
                    holdings[htype] = {"error": str(e)}
        return {"holdings_summary": summary, "holdings": holdings}

    # ---------- risk & rating ----------
    def _risk_probe(self):
        By = self.S["By"]
        els = self.d.find_elements(By.CSS_SELECTOR, RISK_TABLE_CSS)
        return clean(els[0].get_attribute("textContent")) if els else None

    def read_risk_volatility(self):
        By = self.S["By"]
        els = self.d.find_elements(By.CSS_SELECTOR, RISK_TABLE_CSS)
        if not els:
            return {}
        tbl = els[0]
        cols = [clean(th.get_attribute("textContent"))
                for th in tbl.find_elements(By.CSS_SELECTOR, "thead th")]
        out = {}
        for tr in tbl.find_elements(By.CSS_SELECTOR, "tbody tr"):
            ths = tr.find_elements(By.CSS_SELECTOR, "th")
            if not ths:
                continue
            metric = normalize_metric(ths[0].get_attribute("textContent"))
            vals = {}
            for i, td in enumerate(tr.find_elements(By.CSS_SELECTOR, "td")):
                col = cols[i + 1] if i + 1 < len(cols) else f"col{i + 1}"
                vals[col] = clean(td.get_attribute("textContent"))
            out[metric] = vals
        return out

    def read_market_volatility(self):
        By = self.S["By"]
        out = {"capture_ratios": {}, "drawdown": {}, "drawdown_dates": {}}
        els = self.d.find_elements(By.CSS_SELECTOR, MARKET_TABLE_CSS)
        if els:
            for tr in els[0].find_elements(By.CSS_SELECTOR, "tbody tr"):
                ths = tr.find_elements(By.CSS_SELECTOR, "th")
                if not ths:
                    continue
                name = clean(ths[0].get_attribute("textContent"))
                tds = [clean(td.get_attribute("textContent"))
                       for td in tr.find_elements(By.CSS_SELECTOR, "td")]
                if name in ("Upside", "Downside"):
                    out["capture_ratios"][name] = dict(
                        zip(("Investment", "Category", "Index"), tds))
                elif name == "Maximum":
                    out["drawdown"]["Maximum"] = dict(
                        zip(("Investment %", "Category %", "Index %"), tds))
        try:
            dt = self.d.find_element(
                By.XPATH, "//table[thead//th[normalize-space()='Drawdown Dates']]")
            tds = [clean(td.get_attribute("textContent"))
                   for td in dt.find_elements(By.CSS_SELECTOR, "tbody td")]
            out["drawdown_dates"] = dict(zip(("Peak", "Valley", "Max Duration"), tds[-3:]))
        except Exception:
            pass
        return out

    def read_all_risk_years(self):
        By = self.S["By"]
        out = {}
        for btn_id, key in YEAR_TABS:
            btns = self.d.find_elements(By.ID, btn_id)
            if not btns:
                continue
            already = btns[0].get_attribute("aria-selected") == "true"
            if not already:
                before = self._risk_probe()
                self._js_click(btns[0])
                self._wait_change(self._risk_probe, before)
            out[key] = {"risk_volatility_measures": self.read_risk_volatility(),
                        "market_volatility_measures": self.read_market_volatility()}
        return out


# ===========================================================================
# 3) ORCHESTRATOR — modes, workers, merging, manifest
# ===========================================================================

class MorningstarScraper:
    """One scraper, three modes: single fund / single house / full universe.
    Houses are dealt round-robin across worker browsers for the LIST phase;
    (house, fund, url) tasks are dealt round-robin for the ENRICH phase.
    All shared-state merges are lock-protected with atomic file writes."""

    def __init__(self, out_dir, headless=True, delay=ACTION_DELAY,
                 workers=1, max_pages=MAX_PAGES_GUARD, limit=None):
        self.out_dir = out_dir
        self.headless = headless
        self.delay = delay
        self.workers = max(1, int(workers))
        self.max_pages = max_pages
        self.limit = limit
        self._lock = threading.Lock()
        self.house_data = {}     # house -> {fund: attrs}
        self.house_urls = {}     # house -> {fund: href}
        self.failed_houses = {}
        self.fund_failures = {}  # house -> {fund: err}

    def _house_path(self, house):
        return os.path.join(self.out_dir, f"{safe_house_name(house)}.json")

    # ---------------- discovery ----------------
    def discover_houses(self):
        d = make_driver(self.headless)
        try:
            page = FactsheetPage(d, self.delay)
            page.open_factsheet()
            filters = page.read_filter_options()
            atomic_write_json(os.path.join(self.out_dir, "filters.json"), filters)
            return [h for h in filters["fund_house"]
                    if not h.lower().startswith("all ")]
        finally:
            d.quit()

    # ---------------- list phase ----------------
    def list_house(self, page, house):
        """Scrape one house's full list: rows AND detail URLs in one pass."""
        page.set_filters(house)
        page.click_go()
        page.set_rowcount_100()
        headers = page.read_headers()
        data, urls, pages = {}, {}, 0
        while True:
            pages += 1
            if pages > self.max_pages:
                raise RuntimeError(f"pagination guard tripped for '{house}'")
            rows, page_urls = page.read_rows_and_urls()
            data.update(rows_to_json(headers, rows))
            for k, v in page_urls.items():
                urls.setdefault(k, v)
            page.dismiss_popups()
            if not page.next_page():
                break
        return data, urls

    def _list_worker(self, worker_id, houses):
        d = make_driver(self.headless)
        try:
            page = FactsheetPage(d, self.delay)
            page.open_factsheet()
            for house in houses:
                last_err = None
                for attempt in (1, 2):
                    try:
                        fresh, urls = self.list_house(page, house)
                        last_err = None
                        break
                    except Exception as e:
                        last_err = e
                        print(f"  [w{worker_id}] list '{house}' attempt "
                              f"{attempt} failed: {e}", flush=True)
                        try:
                            page.open_factsheet()
                        except Exception:
                            d.quit()
                            d = make_driver(self.headless)
                            page = FactsheetPage(d, self.delay)
                            page.open_factsheet()
                if last_err is not None:
                    with self._lock:
                        self.failed_houses[house] = str(last_err)
                    continue
                path = self._house_path(house)
                with self._lock:
                    existing = {}
                    if os.path.exists(path):
                        try:
                            with open(path, encoding="utf-8") as f:
                                existing = json.load(f)
                        except Exception:
                            existing = {}
                    merged = merge_list_preserving_enrichment(existing, fresh)
                    self.house_data[house] = merged
                    self.house_urls[house] = urls
                    atomic_write_json(path, merged)
                kept = sum(1 for v in merged.values() if "risk_ratings" in v)
                print(f"  [list] {house}: {len(merged)} funds "
                      f"({kept} previously enriched preserved)", flush=True)
        finally:
            try:
                d.quit()
            except Exception:
                pass

    # ---------------- enrich phase ----------------
    def enrich_fund(self, dp, href):
        tabs = derive_tab_urls(href)
        dp.open(tabs["detailed_portfolio"], wait_css=".sal-dp-pair")
        portfolio = dp.read_detailed_portfolio()
        dp.open(tabs["risk_ratings"], wait_css=RISK_TABLE_CSS)
        risk = dp.read_all_risk_years()
        return portfolio, risk

    def _enrich_worker(self, worker_id, tasks):
        d = make_driver(self.headless)
        try:
            dp = FundDetailPage(d, self.delay)
            for house, name, href in tasks:
                last_err = None
                for attempt in (1, 2):
                    try:
                        portfolio, risk = self.enrich_fund(dp, href)
                        last_err = None
                        break
                    except Exception as e:
                        last_err = e
                        print(f"  [w{worker_id}] enrich '{name}' attempt "
                              f"{attempt} failed: {e}", flush=True)
                        try:
                            d.quit()
                        except Exception:
                            pass
                        d = make_driver(self.headless)
                        dp = FundDetailPage(d, self.delay)
                if last_err is not None:
                    with self._lock:
                        self.fund_failures.setdefault(house, {})[name] = str(last_err)
                    continue
                with self._lock:
                    self.house_data[house][name] = nest_fund_details(
                        self.house_data[house][name], detail_url=href,
                        detailed_portfolio=portfolio, risk_ratings=risk,
                        enriched_at=datetime.now(timezone.utc).isoformat())
                    atomic_write_json(self._house_path(house), self.house_data[house])
                eq = portfolio["holdings"].get("Equity")
                bd = portfolio["holdings"].get("Bond")
                print(f"  [enrich] {name}: equity="
                      f"{len(eq) if isinstance(eq, list) else 'ERR'} bond="
                      f"{len(bd) if isinstance(bd, list) else 'ERR'} "
                      f"risk={sorted(risk)}", flush=True)
        finally:
            try:
                d.quit()
            except Exception:
                pass

    # ---------------- manifest ----------------
    def write_manifest(self):
        combined = {}
        with self._lock:
            for house in sorted(self.house_data):
                merge_house_into(combined, self.house_data[house])
            manifest = {
                "source": BASE_URL,
                "scraped_at": datetime.now(timezone.utc).isoformat(),
                "workers": self.workers,
                "fund_houses": {h: len(d) for h, d in sorted(self.house_data.items())},
                "enriched_funds": sum(
                    1 for d in self.house_data.values()
                    for v in d.values() if "risk_ratings" in v),
                "failed_fund_houses": dict(sorted(self.failed_houses.items())),
                "fund_failures": {h: dict(sorted(f.items()))
                                  for h, f in sorted(self.fund_failures.items())},
                "total_schemes": len(combined),
                "payload_sha256": payload_hash(combined),
                "note": ("Live-site snapshot: downstream framework runs pin this "
                         "file by hash; the site itself changes over time."),
            }
            atomic_write_json(
                os.path.join(self.out_dir, "morningstar_factsheet.json"),
                {"manifest": manifest, "funds": combined})
        return manifest

    # ---------------- run ----------------
    def run(self, houses=None, funds=None, enrich=True):
        os.makedirs(self.out_dir, exist_ok=True)

        all_houses = self.discover_houses()
        if houses:
            wanted = {h.lower() for h in houses}
            targets = [h for h in all_houses if h.lower() in wanted]
            missing = wanted - {h.lower() for h in targets}
            if missing:
                raise SystemExit(
                    f"unknown fund house(s) {sorted(missing)} — names must match "
                    f"ms_data/filters.json exactly")
        else:
            targets = all_houses

        # Phase A: list scrape, houses round-robin across workers
        n = min(self.workers, len(targets)) or 1
        buckets = [targets[i::n] for i in range(n)]
        print(f"LIST phase: {len(targets)} house(s) across {n} browser(s)...",
              flush=True)
        with ThreadPoolExecutor(max_workers=n) as pool:
            futs = [pool.submit(self._list_worker, i, b)
                    for i, b in enumerate(buckets) if b]
            for f in as_completed(futs):
                f.result()
        self.write_manifest()

        if not enrich:
            return self.house_data

        # Phase B: enrichment tasks round-robin across workers
        tasks, no_url = [], []
        fund_filter = {f for f in (funds or [])}
        for house in sorted(self.house_data):
            names = sorted(self.house_data[house])
            if fund_filter:
                names = [x for x in names if x in fund_filter]
            if self.limit:
                names = names[: self.limit]
            for name in names:
                href = (self.house_urls.get(house) or {}).get(name)
                if href:
                    tasks.append((house, name, href))
                else:
                    no_url.append(name)
        if fund_filter:
            found = {t[1] for t in tasks}
            for f in sorted(fund_filter - found):
                print(f"  SKIPPED [{f}]: not found in the scraped list", flush=True)
        if no_url:
            print(f"  {len(no_url)} fund(s) had no detail URL and were skipped",
                  flush=True)
        est_min = len(tasks) * SECONDS_PER_FUND_EST / max(1, self.workers) / 60
        print(f"ENRICH phase: {len(tasks)} fund(s) across {self.workers} "
              f"browser(s) (~{est_min:.0f} min estimated)...", flush=True)
        n = min(self.workers, len(tasks)) or 1
        buckets = [tasks[i::n] for i in range(n)]
        with ThreadPoolExecutor(max_workers=n) as pool:
            futs = [pool.submit(self._enrich_worker, i, b)
                    for i, b in enumerate(buckets) if b]
            for f in as_completed(futs):
                f.result()

        manifest = self.write_manifest()
        print(f"done: {manifest['total_schemes']} schemes, "
              f"{manifest['enriched_funds']} enriched, "
              f"{len(manifest['failed_fund_houses'])} house failures, "
              f"{sum(len(v) for v in manifest['fund_failures'].values())} fund failures",
              flush=True)
        return self.house_data


# ===========================================================================
# CLI — three generation modes, few options
# ===========================================================================

def main():
    ap = argparse.ArgumentParser(
        description="Morningstar IN scraper — single canonical script: "
                    "fund list + per-fund details, per-house JSON output")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--house", action="append",
                      help="full report for this fund house (exact name from "
                           "ms_data/filters.json; repeatable)")
    mode.add_argument("--all", action="store_true",
                      help="full universe: every fund house")
    ap.add_argument("--fund", action="append",
                    help="restrict enrichment to specific fund name(s) within "
                         "--house (exact name; repeatable)")
    ap.add_argument("--out", default="ms_data")
    ap.add_argument("--workers", type=int, default=4,
                    help="parallel browser instances (default 4). Higher = "
                         "faster but N× the request rate; be polite.")
    ap.add_argument("--limit", type=int, default=None,
                    help="cap enriched funds per house (testing aid)")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--delay", type=float, default=ACTION_DELAY)
    args = ap.parse_args()

    if args.fund and not args.house:
        ap.error("--fund requires --house")

    scraper = MorningstarScraper(args.out, headless=args.headless,
                                 delay=max(args.delay, 1.0),
                                 workers=args.workers, limit=args.limit)
    scraper.run(houses=args.house, funds=args.fund)


if __name__ == "__main__":
    main()
