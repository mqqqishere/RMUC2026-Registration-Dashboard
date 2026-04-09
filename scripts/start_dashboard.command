#!/bin/zsh
SCRIPT_DIR="${0:A:h}"
cd "${SCRIPT_DIR}/.."
python3 scripts/start_dashboard.py "$@"
