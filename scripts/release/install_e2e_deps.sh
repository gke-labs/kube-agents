#!/usr/bin/env bash
# Installs Python E2E test dependencies.
set -euo pipefail

echo "======================================================================"
echo "📦 INSTALLING PYTHON E2E DEPENDENCIES"
echo "======================================================================"
python -m pip install --upgrade pip
pip install -r tests/e2e/requirements.txt
