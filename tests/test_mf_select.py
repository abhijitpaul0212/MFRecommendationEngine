"""Pytest wrapper around the framework's built-in deterministic selftest.

The selftest builds a fully synthetic (no-RNG) universe and asserts:
  1. two independent runs produce identical run_hash (determinism)
  2. the weak fund fails the alpha-vs-category gate
  3. the crash fund fails the rolling/drawdown gates
  4. the 65%-overlap pair never co-exists in the selection
  5. every selected pairwise overlap <= the configured threshold
  6. changing the config changes config_hash

Run: python -m pytest tests/ -v
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "selection"))

import mf_select  # noqa: E402


def test_selftest_passes():
    assert mf_select.selftest() == 0


def test_percentile_ranks_deterministic_ties():
    ranks = mf_select.percentile_ranks([("a", 1.0), ("b", 1.0), ("c", 2.0), ("d", None)])
    assert ranks["a"] == ranks["b"]          # ties share average rank
    assert ranks["c"] > ranks["a"]
    assert ranks["d"] == 0.0                 # missing metric -> floor, never crashes


def test_pairwise_overlap():
    a = {"INE001": 40.0, "INE002": 60.0}
    b = {"INE001": 10.0, "INE003": 90.0}
    assert mf_select.pairwise_overlap(a, b) == 10.0
    assert mf_select.pairwise_overlap(a, {"INE009": 100.0}) == 0.0
