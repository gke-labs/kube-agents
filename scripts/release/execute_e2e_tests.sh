#!/usr/bin/env bash
# ==============================================================================
# Release Candidate Step 3 Wrapper: Execute Promotion E2E Tests
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "${SCRIPT_DIR}/execute_e2e_tests.py" "$@"
