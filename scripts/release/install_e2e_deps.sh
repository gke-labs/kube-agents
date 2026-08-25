#!/usr/bin/env bash
# Installs Python E2E test dependencies.
set -euo pipefail

echo "======================================================================"
echo "📦 INSTALLING PYTHON E2E DEPENDENCIES"
echo "======================================================================"
if [ -d "bench/.venv" ] && command -v uv &>/dev/null; then
    # Local developer workstation: install into bench/.venv
    uv pip install --python bench/.venv -r tests/e2e/requirements.txt
else
    # CI Runner: install into active Python environment
    python3 -m pip install --upgrade pip
    python3 -m pip install -r tests/e2e/requirements.txt
fi


