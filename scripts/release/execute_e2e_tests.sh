#!/usr/bin/env bash
# Runs the pytest E2E test suite for Google Chat platform agent.
set -euo pipefail

echo "======================================================================"
echo "🧪 EXECUTING E2E TEST SUITE"
echo "======================================================================"
pytest tests/e2e/gchat_agent_test.py -v -s
