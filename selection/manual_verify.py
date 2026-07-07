#!/usr/bin/env python3
"""
Stage 3.5 — MANUAL VERIFICATION GATE
====================================

The deterministic engine (Stage 2) and the NAV rolling-return check (Stage 3)
cannot see two things Morningstar's public pages don't expose in a scrapeable
form: a fund's SORTINO ratio versus its category, and how long the CURRENT
manager has actually run the scheme. The engine therefore prints a per-fund
"Manual verification before investing" checklist and stops there.

This stage closes that loop. The user gathers those figures by hand (Value
Research / Rupeevest / the AMC factsheet) and records them in a small JSON
file; this script applies FIXED thresholds and emits a per-fund verdict
(VERIFIED / REVIEW / WEAK / REJECT). Like every other gate in this repo it can
only ever TIGHTEN the recommendation — it never promotes or invents a pick.

Design contract (mirrors mf_recommend.py / mf_allocate.py)
----------------------------------------------------------
- Pure, deterministic: identical (manual JSON, thresholds) -> identical report
  and identical `manual_hash`. No network, no RNG, no wall-clock in the hash.
- Dates are explicit inputs (`as_of`, manager `since`) so tenure is
  reproducible — never `date.today()`.
- Verdicts are worst-of over the individual checks; the rules live in one
  THRESHOLDS block so they are auditable and testable.

Usage
=====
  # 1) scaffold a template pre-filled with the funds from a recommendation run
  python selection/manual_verify.py --init --report recommendation_run/recommendations.json \
      --out recommendation_run/manual_verification.json

  # 2) (user fills in sortino + manager fields), then assess
  python selection/manual_verify.py --input recommendation_run/manual_verification.json \
      --report recommendation_run/recommendations.json \
      --out recommendation_run/manual_verification_report.json
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import date


# --- Fixed, auditable thresholds --------------------------------------------
THRESHOLDS = {
    "sortino": {
        # fund 5Y Sortino must clear its own benchmark (else the fund adds no
        # downside-risk-adjusted value over the index) and is EXPECTED to clear
        # the category average (else it merely lags its peers).
        "must_beat_benchmark": True,
        "should_beat_category": True,
    },
    "manager_tenure_years": {
        "strong": 5.0,   # has run the scheme through a full cycle
        "ok": 3.0,       # meaningful ownership of the track record
        "flag_below": 2.0,  # too short to attribute the record to this manager
    },
}

VERDICT_ORDER = ["VERIFIED", "REVIEW", "WEAK", "REJECT"]  # best -> worst


def tenure_years(since_iso, as_of_iso):
    """Whole-year tenure (r2) of the current manager as of `as_of`. Both dates
    are explicit ISO strings, so this is fully reproducible."""
    if not since_iso or not as_of_iso:
        return None
    s = date.fromisoformat(since_iso)
    a = date.fromisoformat(as_of_iso)
    return round((a - s).days / 365.25, 2)


def assess_sortino(fund, benchmark, category):
    """(status, detail). FAIL if it can't beat its benchmark; FLAG if it beats
    the benchmark but lags the category; PASS if it clears both."""
    if fund is None or benchmark is None:
        return ("UNKNOWN", "sortino inputs missing — not verified")
    if THRESHOLDS["sortino"]["must_beat_benchmark"] and fund <= benchmark:
        return ("FAIL",
                f"Sortino {fund} does NOT beat its benchmark {benchmark} — "
                f"the fund adds no downside-risk-adjusted edge over the index")
    if (THRESHOLDS["sortino"]["should_beat_category"]
            and category is not None and fund < category):
        return ("FLAG",
                f"Sortino {fund} beats the benchmark {benchmark} but LAGS the "
                f"category average {category} — mediocre among its peers")
    return ("PASS",
            f"Sortino {fund} beats both benchmark {benchmark} and category "
            f"{category}")


def assess_tenure(years, experience_note=""):
    """(status, detail). Tiered on years running THIS scheme; a long total
    industry experience is surfaced as a mitigant but does not upgrade the
    tier (the record still isn't THIS manager's)."""
    th = THRESHOLDS["manager_tenure_years"]
    if years is None:
        return ("UNKNOWN", "manager start date missing — not verified")
    mit = f" (mitigant: {experience_note})" if experience_note else ""
    if years >= th["strong"]:
        return ("STRONG", f"{years}y running the scheme (>= {th['strong']}y — "
                          f"owns a full cycle)")
    if years >= th["ok"]:
        return ("OK", f"{years}y running the scheme (>= {th['ok']}y)")
    if years >= th["flag_below"]:
        return ("CAUTION",
                f"{years}y running the scheme (< {th['ok']}y — short){mit}")
    return ("FLAG",
            f"{years}y running the scheme (< {th['flag_below']}y — very short; "
            f"the track record predates this manager){mit}")


def fund_verdict(sortino_status, tenure_status):
    """Worst-of combination into a single per-fund verdict.
    - REJECT : the fund can't even beat its own benchmark (Sortino FAIL).
    - WEAK   : below-category Sortino AND a very-short (<2y) manager — two
               structural concerns at once.
    - REVIEW : exactly one soft concern (below-category Sortino, OR a short
               manager tenure).
    - VERIFIED: clean on both."""
    if sortino_status == "FAIL":
        return "REJECT"
    sortino_soft = sortino_status == "FLAG"
    tenure_soft = tenure_status in ("CAUTION", "FLAG")
    if sortino_soft and tenure_status == "FLAG":
        return "WEAK"
    if sortino_soft or tenure_soft:
        return "REVIEW"
    if sortino_status == "UNKNOWN" or tenure_status == "UNKNOWN":
        return "REVIEW"      # unverified is not the same as clean
    return "VERIFIED"


def build_assessment(manual, thresholds=None):
    """Pure core: manual-verification dict -> ordered assessment list + rollup.
    `manual` is the value under the top-level `manual_verification` key."""
    as_of = manual.get("as_of")
    rows = []
    for f in manual.get("funds", []):
        s = f.get("sortino") or {}
        m = f.get("manager") or {}
        yrs = tenure_years(m.get("since"), as_of)
        s_status, s_detail = assess_sortino(
            s.get("fund"), s.get("benchmark"), s.get("category"))
        t_status, t_detail = assess_tenure(yrs, m.get("experience_note", ""))
        rows.append({
            "fund": f.get("fund"),
            "bucket": f.get("bucket"),
            "sortino_check": {"status": s_status, "detail": s_detail,
                              "fund": s.get("fund"),
                              "benchmark": s.get("benchmark"),
                              "category": s.get("category")},
            "manager_check": {"status": t_status, "detail": t_detail,
                              "name": m.get("name"),
                              "since": m.get("since"),
                              "tenure_years": yrs},
            "verdict": fund_verdict(s_status, t_status),
        })
    rows.sort(key=lambda r: (VERDICT_ORDER.index(r["verdict"]),
                             r.get("fund") or ""))
    worst = (max((VERDICT_ORDER.index(r["verdict"]) for r in rows), default=0))
    return {
        "as_of": as_of,
        "verified_run_hash": manual.get("run_hash"),
        "rows": rows,
        "portfolio_verdict": VERDICT_ORDER[worst] if rows else "VERIFIED",
        "clean": all(r["verdict"] == "VERIFIED" for r in rows),
    }


def manual_hash(assessment):
    """Deterministic digest of the assessment payload (verdicts + inputs)."""
    payload = json.dumps(assessment, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# --- template scaffolding ---------------------------------------------------
def collect_recommendation_funds(report):
    """Extract the selected funds from either the legacy `bench` layout or the
    newer `recommendations` layout used by the recommendation engine."""
    if not report:
        return []

    if isinstance(report.get("recommendations"), list):
        recs = report.get("recommendations", [])
        out = []
        for rec in recs:
            if isinstance(rec, dict):
                fund = rec.get("fund") or rec.get("pick") or rec.get("name")
                if fund:
                    out.append({
                        "fund": fund,
                        "bucket": rec.get("bucket") or rec.get("bucket_name") or "",
                    })
        if out:
            return out

    bench = report.get("bench") or []
    out = []
    seen = set()
    for item in bench:
        if not isinstance(item, dict):
            continue
        fund = item.get("pick") or item.get("fund") or item.get("name")
        if not fund:
            continue
        key = (fund, item.get("bucket") or "")
        if key in seen:
            continue
        seen.add(key)
        out.append({"fund": fund, "bucket": item.get("bucket") or ""})
    return out


def sanitize_filename(name):
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", name or "fund")
    safe = safe.strip("._-") or "fund"
    return safe


def make_template(report):
    """Build a blank manual-verification JSON pre-filled with the picks from a
    recommendations.json — the user fills the sortino/manager fields."""
    funds = []
    for rec in collect_recommendation_funds(report):
        funds.append({
            "fund": rec.get("fund"),
            "bucket": rec.get("bucket"),
            "sortino": {"fund": None, "benchmark": None, "category": None,
                        "benchmark_name": "", "category_name": ""},
            "manager": {"name": "", "since": "", "experience_note": ""},
        })
    return {
        "manual_verification": {
            "as_of": "",
            "verified_by": "",
            "run_hash": report.get("run_hash"),
            "sources": ["Value Research / Rupeevest / AMC factsheet / Morningstar"],
            "thresholds": THRESHOLDS,
            "_field_help": {
                "sortino.fund": "the fund's 5Y Sortino ratio",
                "sortino.benchmark": "the fund's stated benchmark's Sortino",
                "sortino.category": "the category-average Sortino",
                "manager.since": "ISO date (YYYY-MM-DD) the CURRENT lead "
                                 "manager took over THIS scheme",
                "manager.experience_note": "total industry experience — a "
                                           "mitigant for a short scheme tenure",
            },
            "funds": funds,
        }
    }


def resolve_scraper_html_fixture(fund_name, html_file=None, base_dir=None):
    """Return an HTML fixture path for the scraper when the caller did not
    provide a real VRO URL. The batch flow uses the bundled fixture in
    recommendation_run/ when present so the command still runs in this repo."""
    if html_file:
        return html_file

    roots = []
    if base_dir:
        roots.append(base_dir)
    roots.append(os.getcwd())
    for root in roots:
        if not root:
            continue
        candidate_dir = os.path.join(root, "recommendation_run")
        if os.path.isdir(candidate_dir):
            matches = sorted(glob.glob(os.path.join(candidate_dir, "vro*.html")))
            if matches:
                return matches[0]
    return None


def build_auto_manual_verification(report, as_of, out_dir, scraper_script,
                                  playwright=False, no_headless=False,
                                  user_data_dir=None, selenium=False,
                                  html_file=None, verified_by=""):
    """Iteratively fetch each recommended fund's Value Research data and build
    the `manual_verification` JSON expected by the assessment step."""
    funds = collect_recommendation_funds(report)
    if not funds:
        raise ValueError("no funds found in report")

    os.makedirs(out_dir, exist_ok=True)
    manual = make_template(report)
    manual_verification = manual["manual_verification"]
    manual_verification["as_of"] = as_of
    manual_verification["verified_by"] = verified_by or "automated"
    manual_verification["funds"] = []

    for fund_rec in funds:
        fund_name = fund_rec.get("fund")
        bucket = fund_rec.get("bucket") or ""
        out_path = os.path.join(out_dir, f"mv_{sanitize_filename(fund_name)}.json")
        resolved_html = resolve_scraper_html_fixture(
            fund_name, html_file=html_file,
            base_dir=os.path.dirname(os.path.dirname(__file__)))
        cmd = [sys.executable, scraper_script, "--name", fund_name,
               "--bucket", bucket, "--as-of", as_of, "--out", out_path]
        if playwright:
            cmd.append("--playwright")
        if no_headless:
            cmd.append("--no-headless")
        if selenium:
            cmd.append("--selenium")
        if user_data_dir:
            cmd.extend(["--user-data-dir", user_data_dir])
        if resolved_html:
            cmd.extend(["--html-file", resolved_html])

        print(f"fetching {fund_name} -> {out_path}")
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            print(proc.stderr.strip() or proc.stdout.strip(), file=sys.stderr)
            manual_verification["funds"].append({
                "fund": fund_name,
                "bucket": bucket,
                "sortino": {"fund": None, "benchmark": None, "category": None,
                            "benchmark_name": "", "category_name": ""},
                "manager": {"name": "", "since": "", "experience_note": ""},
                "_fetch_error": proc.stderr.strip() or proc.stdout.strip(),
            })
            continue

        with open(out_path, encoding="utf-8") as f:
            entry = json.load(f)
        manual_verification["funds"].append(entry)

    return {"manual_verification": manual_verification}


def write_assessment(assessment, out_path):
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(assessment, f, indent=2, ensure_ascii=False)
    md_path = out_path.rsplit(".", 1)[0] + ".md"
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(render_md(assessment))
    return out_path, md_path


def render_md(assessment, engine_version=""):
    L = []
    L.append("# Manual verification — Stage 3.5\n")
    L.append(f"- as_of: `{assessment['as_of']}`  |  verified run_hash: "
             f"`{(assessment.get('verified_run_hash') or '')[:16]}…`")
    L.append(f"- **portfolio verdict: {assessment['portfolio_verdict']}**  "
             f"(worst of the per-fund verdicts)\n")
    L.append("| Fund | Bucket | Sortino check | Manager check | Verdict |")
    L.append("|---|---|---|---|---|")
    for r in assessment["rows"]:
        sc, mc = r["sortino_check"], r["manager_check"]
        L.append(f"| {r['fund']} | {r['bucket']} | "
                 f"{sc['status']} | {mc['status']} ({mc['tenure_years']}y) | "
                 f"**{r['verdict']}** |")
    L.append("")
    for r in assessment["rows"]:
        L.append(f"### {r['fund']} — {r['verdict']}")
        L.append(f"- Sortino: {r['sortino_check']['detail']}")
        L.append(f"- Manager: {r['manager_check']['detail']}")
        L.append("")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description="Stage 3.5 manual verification gate")
    ap.add_argument("--report", help="recommendations.json (for --init or to "
                                      "cross-check fund names)")
    ap.add_argument("--input", help="filled manual-verification JSON")
    ap.add_argument("--out", help="output path")
    ap.add_argument("--report-out", help="output path for the generated report")
    ap.add_argument("--init", action="store_true",
                    help="write a blank template from --report to --out")
    ap.add_argument("--auto", action="store_true",
                    help="iteratively fetch Value Research data for each fund in "
                         "--report and write both the filled manual verification "
                         "JSON and the assessment report")
    ap.add_argument("--as-of", help="YYYY-MM-DD used for tenure math")
    ap.add_argument("--scraper-script", default="scraper/valueresearch.py",
                    help="path to scraper/valueresearch.py")
    ap.add_argument("--playwright", action="store_true",
                    help="pass --playwright to scraper/valueresearch.py")
    ap.add_argument("--selenium", action="store_true",
                    help="pass --selenium to scraper/valueresearch.py")
    ap.add_argument("--no-headless", action="store_true",
                    help="pass --no-headless to scraper/valueresearch.py")
    ap.add_argument("--user-data-dir",
                    help="persistent browser profile dir to reuse a logged-in "
                         "VRO session")
    ap.add_argument("--html-file",
                    help="parse a saved HTML file instead of fetching")
    args = ap.parse_args()

    if args.init:
        if not args.report:
            ap.error("--init needs --report")
        with open(args.report, encoding="utf-8") as f:
            report = json.load(f)
        tpl = make_template(report)
        out = args.out or "manual_verification.json"
        with open(out, "w", encoding="utf-8") as f:
            json.dump(tpl, f, indent=2, ensure_ascii=False)
        print(f"template ({len(tpl['manual_verification']['funds'])} funds) "
              f"-> {out}")
        return

    if args.auto:
        if not args.report or not args.as_of:
            ap.error("--auto needs --report and --as-of")
        with open(args.report, encoding="utf-8") as f:
            report = json.load(f)
        out = args.out or "manual_verification.json"
        out_dir = os.path.dirname(os.path.abspath(out)) or "."
        doc = build_auto_manual_verification(
            report, args.as_of, out_dir, args.scraper_script,
            playwright=args.playwright, no_headless=args.no_headless,
            user_data_dir=args.user_data_dir, selenium=args.selenium,
            html_file=args.html_file, verified_by="automated")
        with open(out, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=2, ensure_ascii=False)
        assessment = build_assessment(doc["manual_verification"])
        assessment["manual_hash"] = manual_hash(
            {k: v for k, v in assessment.items() if k != "manual_hash"})
        rep_out = args.report_out or out.rsplit(".", 1)[0] + "_report.json"
        write_assessment(assessment, rep_out)
        print(f"auto manual verification -> {out}")
        print(f"assessment report -> {rep_out}")
        print(f"MANUAL VERIFICATION — portfolio verdict: "
              f"{assessment['portfolio_verdict']}")
        for r in assessment["rows"]:
            print(f"  [{r['verdict']:8s}] {r['fund']}")
            print(f"             sortino: {r['sortino_check']['status']} | "
                  f"manager: {r['manager_check']['status']} "
                  f"({r['manager_check']['tenure_years']}y)")
        print(f"manual_hash={assessment['manual_hash']}")
        sys.exit(0 if assessment["clean"] else 2)

    if not args.input:
        ap.error("need --input (a filled manual-verification JSON)")
    with open(args.input, encoding="utf-8") as f:
        doc = json.load(f)
    manual = doc.get("manual_verification", doc)
    assessment = build_assessment(manual)
    assessment["manual_hash"] = manual_hash(
        {k: v for k, v in assessment.items() if k != "manual_hash"})

    out = args.out or "manual_verification_report.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(assessment, f, indent=2, ensure_ascii=False)
    md = out.rsplit(".", 1)[0] + ".md"
    with open(md, "w", encoding="utf-8") as f:
        f.write(render_md(assessment))

    print(f"MANUAL VERIFICATION — portfolio verdict: "
          f"{assessment['portfolio_verdict']}")
    for r in assessment["rows"]:
        print(f"  [{r['verdict']:8s}] {r['fund']}")
        print(f"             sortino: {r['sortino_check']['status']} | "
              f"manager: {r['manager_check']['status']} "
              f"({r['manager_check']['tenure_years']}y)")
    print(f"report -> {out}  (+ {md})")
    print(f"manual_hash={assessment['manual_hash']}")
    # exit non-zero if anything worse than VERIFIED, so it can gate a pipeline
    sys.exit(0 if assessment["clean"] else 2)


if __name__ == "__main__":
    main()
