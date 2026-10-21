#!/usr/bin/env bash
# WUP Grid Explorer — run from a terminal (Linux) or double-click (macOS: WUP_Explorer.command).
# First run creates .venv and installs the explorer's packages; later runs start in seconds.
set -e
cd "$(dirname "$0")"
PY=${PYTHON:-python3}
if [ ! -x .venv/bin/python ]; then
  echo "Creating a private Python environment in .venv (first run only) ..."
  "$PY" -m venv .venv
fi
if ! cmp -s requirements-explorer.txt .venv/requirements.stamp; then
  echo "Installing the explorer's packages ..."
  .venv/bin/python -m pip install --disable-pip-version-check -q --upgrade pip
  .venv/bin/python -m pip install --disable-pip-version-check -r requirements-explorer.txt
  .venv/bin/python -m pip install --disable-pip-version-check -q --no-deps -e .
  cp requirements-explorer.txt .venv/requirements.stamp
fi
exec .venv/bin/python -m aorc_tools.explorer "$@"
