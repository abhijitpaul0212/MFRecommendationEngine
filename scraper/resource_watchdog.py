#!/usr/bin/env python3
"""
resource_watchdog.py — external resource monitor for a running scrape.

The scraper has an INTERNAL governor (throttles/pauses its own worker slots
when RAM runs low). This watchdog is the OUTER safety net: a separate process
that watches the machine while a scrape runs, keeps a monitoring log alive,
alerts the user when resources fall short, and — only in a true emergency —
freezes the entire scrape process tree (SIGSTOP) until memory recovers, then
thaws it (SIGCONT). A thawed run self-heals: in-flight Selenium calls that
timed out during the freeze fail their attempt, workers restart their
sessions, and the post-run audit re-extracts anything that was lost.

Usage:
  python scraper/resource_watchdog.py                       # defaults
  python scraper/resource_watchdog.py --interval 30 \
      --warn-gb 2.0 --pause-gb 1.0 --resume-gb 3.0 \
      --log ms_data/_resource_monitor.log

Exits 0 when no scrape process remains (run completed).
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from morningstar_fund_details import available_memory_gb  # noqa: E402

SCRAPER_PATTERN = "morningstar_fund_details.py"
CHROME_PATTERNS = ("chromedriver", "chrome.*--headless", "Chrome.*--headless")


def find_pids(pattern):
    try:
        out = subprocess.run(["pgrep", "-f", pattern],
                             capture_output=True, text=True, timeout=5).stdout
        return [int(p) for p in out.split() if int(p) != os.getpid()]
    except Exception:
        return []


def scrape_tree_pids():
    """The scraper python process(es) plus every chromedriver/headless-Chrome
    (on a machine running our scrape these are ours)."""
    pids = find_pids(SCRAPER_PATTERN)
    if not pids:
        return [], []
    chrome = []
    for pat in CHROME_PATTERNS:
        chrome.extend(find_pids(pat))
    return pids, sorted(set(chrome))


def tree_rss_gb(pids):
    if not pids:
        return 0.0
    try:
        out = subprocess.run(
            ["ps", "-o", "rss=", "-p", ",".join(map(str, pids))],
            capture_output=True, text=True, timeout=5).stdout
        return sum(int(x) for x in out.split()) / 1048576.0
    except Exception:
        return -1.0


def notify(msg, title="MF Scraper watchdog"):
    print(f"ALERT: {msg}", flush=True)
    try:
        subprocess.run(["osascript", "-e",
                        f'display notification "{msg}" with title "{title}"'],
                       capture_output=True, timeout=5)
    except Exception:
        pass


def send_signal(pids, sig):
    for pid in pids:
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            pass


def main():
    ap = argparse.ArgumentParser(description="External scrape resource watchdog")
    ap.add_argument("--log", default="ms_data/_resource_monitor.log")
    ap.add_argument("--interval", type=int, default=30)
    ap.add_argument("--warn-gb", type=float, default=2.0,
                    help="alert the user below this much available RAM")
    ap.add_argument("--pause-gb", type=float, default=1.0,
                    help="EMERGENCY: freeze the scrape tree below this")
    ap.add_argument("--resume-gb", type=float, default=3.0,
                    help="thaw a frozen scrape above this")
    args = ap.parse_args()

    # Deduplicate: the scraper auto-starts a watchdog with every run, so
    # overlapping runs would otherwise stack monitors (and double-freeze).
    others = find_pids("resource_watchdog.py")
    if others:
        print(f"watchdog: another instance already running (pid {others[0]}) "
              f"— exiting", flush=True)
        return 0

    state = "OK"          # OK -> WARN -> FROZEN
    frozen = []
    print(f"watchdog: monitoring '{SCRAPER_PATTERN}' every {args.interval}s "
          f"(warn<{args.warn_gb}GB, freeze<{args.pause_gb}GB, "
          f"thaw>{args.resume_gb}GB) -> {args.log}", flush=True)

    while True:
        scraper, chrome = scrape_tree_pids()
        if not scraper and state != "FROZEN":
            line = (f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} "
                    f"watchdog: no scrape process — run completed, exiting")
            print(line, flush=True)
            with open(args.log, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            return 0

        avail = available_memory_gb()
        try:
            load1 = os.getloadavg()[0]
        except OSError:
            load1 = -1.0
        rss = tree_rss_gb(scraper + chrome)
        line = (f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} "
                f"watchdog avail={avail if avail is None else round(avail, 2)}GB "
                f"load1={load1:.1f} scrape_rss={rss:.2f}GB "
                f"procs={len(scraper) + len(chrome)} state={state}")
        try:
            with open(args.log, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass

        if avail is not None:
            if state == "FROZEN":
                if avail >= args.resume_gb:
                    send_signal(frozen, signal.SIGCONT)
                    notify(f"memory recovered ({avail:.1f} GB) — scrape "
                           f"resumed ({len(frozen)} processes thawed)")
                    frozen, state = [], "OK"
            elif avail < args.pause_gb:
                frozen = scraper + chrome
                send_signal(frozen, signal.SIGSTOP)
                notify(f"available RAM {avail:.2f} GB critically low — "
                       f"scrape FROZEN ({len(frozen)} processes) until "
                       f"memory recovers (> {args.resume_gb} GB)")
                state = "FROZEN"
            elif avail < args.warn_gb:
                if state != "WARN":
                    notify(f"available RAM low: {avail:.2f} GB — the scraper's "
                           f"internal governor should be throttling; watch "
                           f"{args.log}")
                state = "WARN"
            else:
                if state == "WARN":
                    notify(f"memory back to {avail:.1f} GB available")
                state = "OK"

        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
