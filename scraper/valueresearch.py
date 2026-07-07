#!/usr/bin/env python3
"""
Value Research Online (VRO) — manual-verification data source (Stage 3.5 feed)
==============================================================================

Morningstar's public pages don't expose a fund's Sortino ratio or the FULL
management team in a scrapeable form, so the Stage 3.5 gate (manual_verify.py)
previously relied on hand-typed figures. This module fetches them from Value
Research and emits the exact `funds[]` entry that manual_verify.py consumes —
turning the manual gate into a repeatable, auditable step.

Two facts are pulled from a fund's VRO page:
  • Risk tab  (#risk)  — the risk table's Sortino column, read row-wise:
        row 1 = the fund, row 2 = its benchmark, row 3 = its category.
        (We also keep Mean/Std/Sharpe/Beta/Alpha for cross-checks.)
  • Other tab (#other) — the Fund Manager panel: EVERY manager on the scheme
        with the date each took it over. This is why VRO matters: a fund can
        be fronted by a junior name yet overseen by a multi-year veteran — the
        team view corrects a "short-tenure" misread that a single name causes.

Design contract (mirrors the rest of the repo)
----------------------------------------------
- PARSING is pure and deterministic (regex over the served HTML); it is unit
  tested against captured fixtures and never touches the network.
- FETCHING is a thin, best-effort I/O layer (urllib + a browser UA); it can be
  bypassed entirely with --html-file for offline / reproducible runs.
- Tenure is computed from an explicit `--as-of` date, never date.today().
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import date, datetime

VRO_FUND_URL = "https://www.valueresearchonline.com/funds/{fund_id}/"


# --- tiny HTML helpers ------------------------------------------------------
def _text(html_fragment):
    """Strip tags, unescape &nbsp;, collapse whitespace."""
    t = re.sub(r"<[^>]+>", " ", html_fragment)
    t = t.replace("&nbsp;", " ").replace("&amp;", "&")
    return re.sub(r"\s+", " ", t).strip()


def _to_float(s):
    s = (s or "").strip()
    if s in ("", "--", "—", "N/A", "NA"):
        return None
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


def parse_vro_date(s):
    """'19-Feb-2025' or 'Feb 2025' -> ISO 'YYYY-MM-DD' (day 1 if absent)."""
    s = (s or "").strip()
    s = re.sub(r"(?i)^since\s+", "", s)
    for fmt in ("%d-%b-%Y", "%d-%B-%Y", "%b %Y", "%B %Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _tenure_years(since_iso, as_of_iso):
    if not since_iso or not as_of_iso:
        return None
    d0 = date.fromisoformat(since_iso)
    d1 = date.fromisoformat(as_of_iso)
    return round((d1 - d0).days / 365.25, 2)


# --- risk table (#risk) -----------------------------------------------------
def parse_risk_table(html):
    """Return {fund,benchmark,category} each a dict of risk metrics, read from
    the first three body rows of VRO's risk table (rows 4+ are Rank / count and
    are ignored). Column positions are discovered from the header, so a column
    re-order on VRO won't silently misread Sortino."""
    tbl = re.search(r'<table[^>]*datatable-fixedheader[^>]*>(.*?)</table>',
                    html, re.S)
    if not tbl:
        return None
    tbl = tbl.group(1)
    thead = re.search(r"<thead>(.*?)</thead>", tbl, re.S)
    tbody = re.search(r"<tbody>(.*?)</tbody>", tbl, re.S)
    if not thead or not tbody:
        return None
    headers = [_text(h).lower()
               for h in re.findall(r"<th[^>]*>(.*?)</th>", thead.group(1), re.S)]

    def col(name):
        for i, h in enumerate(headers):
            if name in h:
                return i
        return None
    idx = {k: col(k) for k in
           ("mean", "std", "sharpe", "sortino", "beta", "alpha")}

    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", tbody.group(1), re.S)
    labels = ("fund", "benchmark", "category")
    out = {}
    for label, row in zip(labels, rows[:3]):
        cells = [_text(td) for td in
                 re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        rec = {"name": cells[0] if cells else ""}
        for metric, i in idx.items():
            rec[metric] = _to_float(cells[i]) if (i is not None
                                                  and i < len(cells)) else None
        out[label] = rec
    return out


# --- fund-manager panel (#other) -------------------------------------------
def parse_fund_managers(html):
    """Every manager on the scheme, in VRO's listed order (first = the primary
    named manager). Each: name, since (ISO), experience, education,
    funds_managed_count."""
    panel = re.search(
        r'<div class="vr-fund-manager-details.*?</div>\s*</div>\s*</div>',
        html, re.S)
    scope = panel.group(0) if panel else html
    managers = []
    # each manager header <p ... data-target="#fund-manager-ID">NAME <span>since ...</span></p>
    for m in re.finditer(
            r'data-target="#fund-manager-(\d+)"[^>]*>(.*?)</p>(.*?)(?=data-target="#fund-manager-\d+"|$)',
            scope, re.S):
        mid, header, body = m.group(1), m.group(2), m.group(3)
        span = re.search(r"<span>(.*?)</span>", header, re.S)
        since_txt = _text(span.group(1)) if span else ""
        # name = header text minus the span and any <img>
        name = _text(re.sub(r"<span>.*?</span>", "", header, flags=re.S))
        exp = re.search(r"Experience:\s*</strong>(.*?)</p>", body, re.S)
        edu = re.search(r"Education:\s*</strong>(.*?)</p>", body, re.S)
        n_funds = len(re.findall(r'class="managed-fund-name"', body))
        managers.append({
            "name": name,
            "since": parse_vro_date(since_txt),
            "since_text": since_txt,
            "experience": _text(exp.group(1)) if exp else "",
            "education": _text(edu.group(1)) if edu else "",
            "funds_managed_count": n_funds,
        })
    return managers


def summarize_managers(managers, as_of):
    """Team roll-up. `primary` = first listed; `longest` = the manager who has
    run THIS scheme the longest (the fund's most experienced hand). The gate
    keys off the longest tenure so a junior-fronted, veteran-overseen fund is
    not mis-flagged as short-tenure — the primary + team size stay visible."""
    if not managers:
        return None
    with_ten = [{**mgr, "tenure_years": _tenure_years(mgr["since"], as_of)}
                for mgr in managers]
    dated = [m for m in with_ten if m["tenure_years"] is not None]
    longest = max(dated, key=lambda m: m["tenure_years"]) if dated else with_ten[0]
    primary = with_ten[0]
    return {
        "primary": primary,
        "longest": longest,
        "team_size": len(managers),
        "team": [{"name": m["name"], "since": m["since"],
                  "tenure_years": m["tenure_years"]} for m in with_ten],
    }


def build_manual_entry(fund_name, bucket, risk, managers, as_of, source_url):
    """Assemble the exact fund dict that manual_verify.py's `funds[]` expects,
    with a `manager` block keyed on the LONGEST-tenured hand plus full team
    context, and provenance for auditability."""
    summ = summarize_managers(managers, as_of)
    sortino = {
        "fund": risk["fund"].get("sortino") if risk else None,
        "benchmark": risk["benchmark"].get("sortino") if risk else None,
        "category": risk["category"].get("sortino") if risk else None,
        "benchmark_name": risk["benchmark"].get("name") if risk else "",
        "category_name": risk["category"].get("name") if risk else "",
    }
    mgr = {}
    if summ:
        lg, pr = summ["longest"], summ["primary"]
        note = (f"team of {summ['team_size']}; primary listed: {pr['name']} "
                f"(since {pr['since']}, {pr['tenure_years']}y). "
                f"{lg.get('experience','')}").strip()
        mgr = {"name": lg["name"], "since": lg["since"],
               "experience_note": note,
               "team_size": summ["team_size"],
               "primary": {"name": pr["name"], "since": pr["since"]},
               "team": summ["team"]}
    return {"fund": fund_name, "bucket": bucket,
            "sortino": sortino, "manager": mgr,
            "_source": {"url": source_url, "as_of": as_of,
                        "risk": risk}}


def fetch_html(url, timeout=20):
    """Plain GET with a browser UA. VRO sits behind Cloudflare and renders the
    risk/manager panels via JS, so this usually returns a challenge page — use
    fetch_html_selenium (or --html-file) for the real content. Kept for
    diagnostics and non-protected pages."""
    import urllib.request
    req = urllib.request.Request(url, headers={
        "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/120.0 Safari/537.36"),
        "Accept-Language": "en-US,en;q=0.9"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def looks_rendered(html):
    """True once the JS-rendered sections we need are actually present."""
    return ("datatable-fixedheader" in html
            and "vr-fund-manager-details" in html)


# VRO throws a newsletter/subscribe modal, a cookie banner and an occasional
# premium-upsell modal that overlay the content. Best-effort closers, tried in
# order; each is optional and never fatal.
_POPUP_CLOSERS = [
    '//button[@id="close-subscribe"]',          # VRO "Assess Risks" subscribe modal
    '//button[@data-dismiss="modal"]',
    '//button[contains(@class,"close") and contains(@class,"dismiss")]',
    '//button[@aria-label="Close" or contains(@class,"close")]',
    '//a[contains(@class,"close") or contains(@class,"modal-close")]',
    '//*[@id="premium-subscribe-modal"]//button[contains(@class,"close")]',
    '//button[contains(.,"No thanks") or contains(.,"Not now") '
    'or contains(.,"Maybe later") or normalize-space(.)="×"]',
    '//button[contains(.,"Accept") or contains(.,"I Agree") '
    'or contains(.,"Got it") or contains(.,"Allow")]',
]


def _dismiss_popups(driver, delay=0.4):
    """Close any overlay/cookie/subscribe popup, then press ESC. Best-effort:
    scans the page (and shallow iframes) and NEVER raises."""
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys

    def close_here():
        closed = False
        for xp in _POPUP_CLOSERS:
            try:
                for el in driver.find_elements(By.XPATH, xp):
                    if el.is_displayed():
                        try:
                            el.click()
                        except Exception:
                            driver.execute_script("arguments[0].click();", el)
                        closed = True
                        time.sleep(delay)
            except Exception:
                continue
        return closed

    try:
        close_here()
        for frame in driver.find_elements(By.TAG_NAME, "iframe")[:5]:
            try:
                driver.switch_to.frame(frame)
                close_here()
            except Exception:
                pass
            finally:
                driver.switch_to.default_content()
        try:
            driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
        except Exception:
            pass
    except Exception:
        pass


def fetch_html_selenium(url, headless=True, settle=3.0, attempts=20,
                        user_data_dir=None):
    """Load the fund page in a real browser (reusing the Morningstar driver),
    clear Cloudflare, open the Risk and Fund-Manager sections, and return the
    rendered page source. `user_data_dir` reuses a persistent (logged-in)
    profile — needed since the panels are account-gated. Best-effort: raises
    if the sections never appear."""
    import os
    sys.path.insert(0, os.path.dirname(__file__))
    from morningstar_fund_details import make_driver  # reuse the hardened driver

    d = make_driver(headless, user_data_dir=user_data_dir)
    try:
        d.set_page_load_timeout(60)
        # Each panel needs a FRESH navigation to its fragment (a same-page hash
        # change won't re-trigger the SPA router); the "Assess Risks" subscribe
        # modal then appears and must be closed. Fetch both and combine.
        parts = []
        for frag, needle in (("#risk", "datatable-fixedheader"),
                             ("#other", "vr-fund-manager-details")):
            try:
                d.get("about:blank")    # force a fresh load (not a hash change)
                d.get(url + frag)
            except Exception:
                pass
            for _ in range(attempts):
                _dismiss_popups(d)      # clear Cloudflare-cleared subscribe modal
                html = d.page_source
                if needle in html:
                    break
                time.sleep(settle)
            parts.append(d.page_source)
        combined = "\n".join(parts)
        if not looks_rendered(combined):
            raise RuntimeError(
                "VRO risk/manager panels did not render (Cloudflare challenge "
                "or JS timeout) — retry, or pass --html-file with saved HTML")
        return combined
    finally:
        try:
            d.quit()
        except Exception:
            pass


_PW_CLOSE_SELECTORS = [
    "#close-subscribe",                     # VRO "Assess Risks" subscribe modal
    "button[data-dismiss='modal']",
    "div.modal.show button.close",
    "button[aria-label='Close']",
    "#premium-subscribe-modal button.close",
    "a.modal-close, a.close",
    "button:has-text('No thanks')", "button:has-text('Not now')",
    "button:has-text('Maybe later')",
    "button:has-text('Accept')", "button:has-text('Got it')",
    "button:has-text('Allow')",
]


def _dismiss_popups_pw(page):
    """Playwright popup closer — same intent as _dismiss_popups, best-effort."""
    for sel in _PW_CLOSE_SELECTORS:
        try:
            for el in page.locator(sel).all():
                if el.is_visible():
                    el.click(timeout=1500)
                    page.wait_for_timeout(300)
        except Exception:
            continue
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass


def fetch_html_playwright(url, headless=True, settle=3.0, attempts=20,
                          user_data_dir=None):
    """Fallback fetch via Playwright — its bundled Chromium clears Cloudflare
    where plain Selenium is flagged. Opens the Risk and Fund-Manager tabs,
    dismisses popups, and returns the rendered HTML.

    `user_data_dir` loads a PERSISTENT browser profile (via
    launch_persistent_context) so a VRO login/session in that directory
    carries into the run — the risk/manager panels are account-gated, so this
    is what makes the live fetch actually return them. Point it at a DEDICATED
    dir (not your main Chrome profile, which Chrome locks while open); on the
    first run use --no-headless and log into VRO once — the session then
    persists there for subsequent runs.

    Raises if the panels never appear (retry with --no-headless / a logged-in
    --user-data-dir) or if Playwright isn't installed."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise RuntimeError(
            "Playwright not installed — run: pip install playwright && "
            "playwright install chromium") from e

    ua = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36")
    viewport = {"width": 1600, "height": 1000}
    persistent = bool(user_data_dir)
    with sync_playwright() as p:
        # persistent profile reuses one context (cookies/login carry over);
        # otherwise each fragment gets its OWN fresh context — opening a second
        # page in a shared context re-triggers a Cloudflare challenge that never
        # clears, but a fresh context clears it every time.
        p_ctx = browser = None
        if persistent:
            p_ctx = p.chromium.launch_persistent_context(
                user_data_dir, headless=headless, user_agent=ua,
                viewport=viewport, locale="en-US")
        else:
            browser = p.chromium.launch(headless=headless)

        def grab(frag, needle):
            own = None
            if persistent:
                page = p_ctx.new_page()
            else:
                own = browser.new_context(user_agent=ua, viewport=viewport,
                                          locale="en-US")
                page = own.new_page()
            try:
                try:
                    page.goto(url + frag, wait_until="domcontentloaded",
                              timeout=60000)
                except Exception:
                    pass
                # poll through Cloudflare + the "Assess Risks" subscribe modal
                # until the panel's marker appears
                for _ in range(attempts):
                    _dismiss_popups_pw(page)
                    html = page.content()
                    if needle in html:
                        return html
                    page.wait_for_timeout(int(settle * 1000))
                return page.content()
            finally:
                try:
                    page.close()
                except Exception:
                    pass
                if own is not None:
                    try:
                        own.close()
                    except Exception:
                        pass

        try:
            combined = "\n".join([
                grab("#risk", "datatable-fixedheader"),
                grab("#other", "vr-fund-manager-details")])
            if not looks_rendered(combined):
                raise RuntimeError(
                    "VRO panels did not render via Playwright "
                    "(Cloudflare not cleared) — retry with --no-headless, "
                    "or use --html-file")
            return combined
        finally:
            try:
                if p_ctx is not None:
                    p_ctx.close()
            except Exception:
                pass
            try:
                if browser is not None:
                    browser.close()
            except Exception:
                pass


def playwright_login(user_data_dir, url="https://www.valueresearchonline.com/login/"):
    """One-time interactive login: open VRO in a visible, persistent-profile
    browser, wait for the user to sign in, then close — the session cookies
    stay in `user_data_dir` for subsequent headless fetches."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir, headless=False,
            viewport={"width": 1400, "height": 900}, locale="en-US")
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except Exception:
            pass
        input("A browser window is open. Log in to Value Research, then press "
              "Enter here to save the session... ")
        try:
            ctx.close()
        except Exception:
            pass
    print(f"session saved to {user_data_dir} — future --selenium/--playwright "
          f"runs with --user-data-dir {user_data_dir} reuse it")


def main():
    ap = argparse.ArgumentParser(
        description="Fetch VRO Sortino + fund-manager team for the Stage 3.5 gate")
    ap.add_argument("--fund-id", help="VRO numeric fund id (e.g. 17361)")
    ap.add_argument("--url", help="full VRO fund URL (overrides --fund-id)")
    ap.add_argument("--html-file", help="parse a saved HTML file instead of "
                                        "fetching (offline / reproducible)")
    ap.add_argument("--selenium", action="store_true",
                    help="fetch via a real browser (needed — VRO is behind "
                         "Cloudflare and renders panels with JS); auto-falls "
                         "back to Playwright if Selenium can't clear Cloudflare")
    ap.add_argument("--playwright", action="store_true",
                    help="fetch via Playwright directly (skip Selenium)")
    ap.add_argument("--no-headless", action="store_true",
                    help="show the browser window (helps clear Cloudflare)")
    ap.add_argument("--user-data-dir",
                    help="persistent browser-profile dir to reuse a logged-in "
                         "VRO session (the risk/manager panels are "
                         "account-gated). Use a DEDICATED dir; run --login "
                         "once first, then reuse it.")
    ap.add_argument("--login", action="store_true",
                    help="one-time: open a visible browser to log into VRO and "
                         "save the session into --user-data-dir, then exit")
    ap.add_argument("--name", help="fund name as used by the recommendation "
                                   "engine (required unless --login)")
    ap.add_argument("--bucket", default="", help="core/growth/aggressive/diversifier")
    ap.add_argument("--as-of", help="YYYY-MM-DD for tenure math (required "
                                    "unless --login)")
    ap.add_argument("--out", help="write the manual_verification funds[] entry "
                                  "here (default: stdout)")
    args = ap.parse_args()

    if args.login:
        if not args.user_data_dir:
            ap.error("--login needs --user-data-dir (where to save the session)")
        playwright_login(args.user_data_dir)
        return
    if not args.name or not args.as_of:
        ap.error("--name and --as-of are required (unless --login)")

    if args.html_file:
        with open(args.html_file, encoding="utf-8") as f:
            html = f.read()
        source = args.html_file
    else:
        url = args.url or (VRO_FUND_URL.format(fund_id=args.fund_id)
                           if args.fund_id else None)
        if not url:
            ap.error("need --url, --fund-id, or --html-file")
        headless = not args.no_headless
        udd = args.user_data_dir
        if args.playwright:
            html = fetch_html_playwright(url, headless=headless,
                                         user_data_dir=udd)
        elif args.selenium:
            try:
                html = fetch_html_selenium(url, headless=headless,
                                           user_data_dir=udd)
            except Exception as e:
                print(f"Selenium fetch failed ({e}); falling back to "
                      f"Playwright...", file=sys.stderr)
                html = fetch_html_playwright(url, headless=headless,
                                             user_data_dir=udd)
        else:
            html = fetch_html(url)
        source = url
        if not looks_rendered(html):
            print("WARNING: fetched HTML lacks the risk/manager panels "
                  "(Cloudflare challenge or JS not rendered). Re-run with "
                  "--selenium / --playwright / --no-headless, or pass "
                  "--html-file with saved page HTML.", file=sys.stderr)

    risk = parse_risk_table(html)
    managers = parse_fund_managers(html)
    entry = build_manual_entry(args.name, args.bucket, risk, managers,
                               args.as_of, source)

    out_json = json.dumps(entry, indent=2, ensure_ascii=False)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(out_json)
        print(f"entry -> {args.out}")
    else:
        print(out_json)

    s = entry["sortino"]
    print(f"\nSortino  fund={s['fund']}  benchmark={s['benchmark']}  "
          f"category={s['category']}", file=sys.stderr)
    if entry["manager"]:
        mg = entry["manager"]
        print(f"Managers team_size={mg['team_size']}  "
              f"longest={mg['name']} (since {mg['since']})  "
              f"primary={mg['primary']['name']} (since {mg['primary']['since']})",
              file=sys.stderr)


if __name__ == "__main__":
    main()
