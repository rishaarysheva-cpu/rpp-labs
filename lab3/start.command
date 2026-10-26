#!/bin/sh
set -eu
RPP_PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$RPP_PROJECT_DIR"
if ! command -v python3 >/dev/null 2>&1; then
    echo 'Install Python 3.12 or newer, then run this file again.'
    exit 1
fi
python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,12) else "Python 3.12 or newer is required")'
if [ ! -x .venv/bin/python ]; then
    python3 -m venv .venv
fi
.venv/bin/python -m pip install -r requirements.txt
exec .venv/bin/python run.py
