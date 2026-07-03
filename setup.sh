#!/usr/bin/env bash
#
# setup.sh — provision a Python virtual environment for MFRecommendationEngine
# and install the scraper dependencies (selenium, webdriver-manager, pytest).
#
# Usage:
#   ./setup.sh                 # create .venv (if missing) and install deps
#   source .venv/bin/activate  # then activate it in your shell
#
# Idempotent: safe to re-run. Re-running just re-installs / upgrades deps into
# the existing .venv.

set -euo pipefail

# Always operate relative to this script's directory (the repo root), so the
# script works no matter where it's invoked from.
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

VENV_DIR="${VENV_DIR:-.venv}"
REQ_FILE="scraper/requirements.txt"

# Pick a Python 3 interpreter (the pure core uses `from __future__` + f-strings;
# the system `python` on macOS may still be 2.7, so prefer python3).
PYTHON_BIN="${PYTHON_BIN:-python3}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "error: '$PYTHON_BIN' not found on PATH. Install Python 3 first." >&2
  exit 1
fi

echo "==> Using $("$PYTHON_BIN" --version 2>&1) at $(command -v "$PYTHON_BIN")"

# 1) Create the virtual environment if it doesn't already exist.
if [ ! -d "$VENV_DIR" ]; then
  echo "==> Creating virtual environment in $VENV_DIR"
  "$PYTHON_BIN" -m venv "$VENV_DIR"
else
  echo "==> Reusing existing virtual environment in $VENV_DIR"
fi

# 2) Install/upgrade dependencies using the venv's own interpreter (no need to
#    pre-activate — calling the venv python directly is enough and keeps the
#    script shell-agnostic).
VENV_PY="$VENV_DIR/bin/python"
echo "==> Upgrading pip"
"$VENV_PY" -m pip install --upgrade pip --quiet

if [ ! -f "$REQ_FILE" ]; then
  echo "error: requirements file not found: $REQ_FILE" >&2
  exit 1
fi

echo "==> Installing dependencies from $REQ_FILE"
"$VENV_PY" -m pip install -r "$REQ_FILE"

echo
echo "==> Done. Activate the environment with:"
echo "      source $VENV_DIR/bin/activate"
echo
echo "    Then, for example:"
echo "      python -m pytest tests/ -v                                   # browserless tests"
echo "      python scraper/morningstar_factsheet.py --out ms_data --headless"
