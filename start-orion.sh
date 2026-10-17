#!/usr/bin/env bash
# Starts ORION. Run:  ./start-orion.sh
# Uses the project's own Python (.venv), picks a free port, and opens the browser.
set -e
cd "$(dirname "$0")"

VENV_PY=".venv/bin/python"

if [ ! -x "$VENV_PY" ]; then
    echo
    echo "ORION's Python environment (the .venv folder) was not found."
    read -r -p "Set it up now? This downloads ORION's packages (internet needed) [y/N] " answer

    case "$answer" in
        [yY]*) ;;
        *)
            echo
            echo "To set it up yourself, run:"
            echo "    python3 -m venv .venv"
            echo "    .venv/bin/python -m pip install -r requirements.txt"
            exit 1
            ;;
    esac

    PY="$(command -v python3 || command -v python || true)"

    if [ -z "$PY" ]; then
        echo "Python 3.10 or newer was not found. Install it, then run this again."
        exit 1
    fi

    "$PY" -m venv .venv
    "$VENV_PY" -m pip install -r requirements.txt
fi

exec "$VENV_PY" launch.py "$@"
